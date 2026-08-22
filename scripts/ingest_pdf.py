#!/usr/bin/env python3
"""Ingest PDF statements (credit card, e-wallet, bank) into transactions.

Pipeline:
  1. Extract text with pdfplumber. If the PDF is scanned (little/no text),
     fall back to OCR (pypdfium2 render -> Tesseract).
  2. Parse transaction rows. The LOCAL parser (default, fully offline) detects
     date + amount + description per line. For layouts it can't handle, the
     opt-in --ai flag sends ONLY the extracted text (never the PDF) to Claude
     to structure the rows.
  3. Categorize via merchant_category_map, dedup, and insert as source
     'statement'. The file is hashed; re-ingesting the same file is skipped.

Privacy: with the local parser nothing leaves the machine. --ai transmits the
extracted statement text to the Anthropic API — an explicit, per-run opt-in.

Usage:
    python scripts/ingest_pdf.py data/statements/bca_2026_08.pdf --dry-run
    python scripts/ingest_pdf.py data/statements/ --type cc --account "BCA Checking"
    python scripts/ingest_pdf.py data/gopay.pdf --ai --type ewallet   # opt-in AI
    python scripts/ingest_pdf.py data/x.pdf --force                    # re-ingest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect            # noqa: E402
from ingest_core import commit_transactions          # noqa: E402
from ingest_sheet import parse_amount, parse_date, load_lookups  # noqa: E402
from categorize import load_map, match_category       # noqa: E402
from pii import redact as redact_text, redact_field    # noqa: E402

AI_MODEL_DEFAULT = os.getenv("PFA_AI_MODEL", "claude-opus-5")
SOURCE_TYPES = ("cc", "ewallet", "bank", "other")

# A date anywhere in a line (ISO, day-first with / . -).
_DATE_RE = re.compile(
    r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b")
# A currency-ish number: 1.250.000  /  75,000  /  12.500,00  /  Rp 5000
_MONEY_RE = re.compile(r"(?:rp\.?\s*)?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?|\d{4,}(?:[.,]\d{2})?)", re.I)
# Credit markers -> treat as income rather than expense. Deliberately strict:
# a standalone 'CR' flag (not the substring in 'CREDIT'), or explicit terms.
_CREDIT_RE = re.compile(
    r"(?:\bCR\b|\brefund\b|\bcashback\b|\breversal\b|\bpembayaran\b|\btop[\s-]?up\b)",
    re.I)

# Indonesian (and a few English) month names -> number, keyed by first 3 letters.
_ID_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6,
    "jul": 7, "agu": 8, "agt": 8, "ags": 8, "aug": 8, "sep": 9, "okt": 10,
    "oct": 10, "nov": 11, "des": 12, "dec": 12,
}
# Day + month-name date, e.g. 06-OKT / 03 NOV / 3 November
_ID_DATE_RE = re.compile(r"\b(\d{1,2})[-\s]([A-Za-z]{3,9})\b")
# A full anchor date somewhere in the header, e.g. 03 NOVEMBER 2025
_FULL_ID_DATE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(20\d{2})\b")


def _statement_anchor(text: str) -> tuple[int, int] | None:
    """(year, month) from the statement header, to supply the year for
    day-month-only transaction dates."""
    for m in _FULL_ID_DATE.finditer(text):
        mon = _ID_MONTHS.get(m.group(2)[:3].lower())
        if mon:
            return int(m.group(3)), mon
    return None


def _strip_id_dates(s: str) -> str:
    """Remove only real day-month tokens (leave things like 'KE 05 DARI')."""
    def repl(m):
        return " " if _ID_MONTHS.get(m.group(2)[:3].lower()) else m.group(0)
    return _ID_DATE_RE.sub(repl, s)


def _find_date(line: str, anchor: tuple[int, int] | None):
    """Return (iso_date, matched_text) using a numeric date, else a
    day-month-name date with the year inferred from `anchor`."""
    m = _DATE_RE.search(line)
    if m:
        d = parse_date(m.group(1))
        if d:
            return d, m.group(0)
    for m in _ID_DATE_RE.finditer(line):
        mon = _ID_MONTHS.get(m.group(2)[:3].lower())
        day = int(m.group(1))
        if not mon or not (1 <= day <= 31):
            continue
        # A full 'DD MONTH YYYY' (year follows) is a header/promo date, not a
        # transaction row (rows are year-less like '06-OKT'). Skip it.
        if re.match(r"\s*20\d{2}\b", line[m.end():m.end() + 6]):
            continue
        if not anchor:
            return None, None      # can't know the year
        year, smonth = anchor
        if mon > smonth:           # e.g. Dec txn on a Jan statement
            year -= 1
        return f"{year:04d}-{mon:02d}-{day:02d}", m.group(0)
    return None, None


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class NeedPassword(Exception):
    """Raised when a PDF is encrypted and no/incorrect password was supplied."""


def is_encrypted(path: Path) -> bool:
    from pypdf import PdfReader
    try:
        return PdfReader(str(path)).is_encrypted
    except Exception:
        return False


def _check_password(path: Path, password: str | None) -> None:
    """Raise NeedPassword unless the PDF opens with `password`."""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if not reader.is_encrypted:
        return
    if not password:
        raise NeedPassword()
    try:
        if reader.decrypt(password) == 0:   # 0 == wrong password
            raise NeedPassword()
    except NeedPassword:
        raise
    except Exception as e:
        raise NeedPassword() from e


def extract_text_pdfplumber(path: Path, password: str | None = None) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(path, password=password or "") as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def ocr_pdf(path: Path, password: str | None = None, lang: str = "ind+eng") -> str:
    """Render each page and OCR it (for scanned statements)."""
    try:
        import pypdfium2 as pdfium
        import pytesseract
    except ImportError as e:
        raise SystemExit("Scanned PDF needs pypdfium2 + pytesseract. "
                         "Run: pip install pypdfium2 pytesseract") from e
    text_parts = []
    pdf = pdfium.PdfDocument(str(path), password=password or None)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            pil = page.render(scale=2.0).to_pil()   # ~144 dpi
            text_parts.append(pytesseract.image_to_string(pil, lang=lang))
    finally:
        pdf.close()
    return "\n".join(text_parts)


def extract_text(path: Path, password: str | None = None) -> tuple[str, str]:
    """Return (text, method). Raises NeedPassword if the PDF is locked and the
    password is missing/wrong. Falls back to OCR when there is no text layer."""
    _check_password(path, password)
    text = extract_text_pdfplumber(path, password)
    if len(text.strip()) >= 40:
        return text, "text"
    return ocr_pdf(path, password), "ocr"


# --------------------------------------------------------------------------
# Local parser
# --------------------------------------------------------------------------

def _money_tokens(line: str) -> list[tuple[str, int]]:
    out = []
    for m in _MONEY_RE.finditer(line):
        val = parse_amount(m.group(1))
        if val is not None:
            out.append((m.group(0), val))
    return out


def parse_statement_local(text: str) -> list[dict]:
    """Best-effort: a line with a date and >=1 amount is a transaction row.

    Takes the LAST money token as the amount (statements usually put it in the
    trailing column). Lines with multiple amounts (e.g. a running-balance
    column) are ambiguous and flagged. Use --ai for layouts this misreads.
    """
    anchor = _statement_anchor(text)
    rows = []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        date, datetok = _find_date(line, anchor)
        if not date:
            continue
        # Strip the transaction date and any posting/second day-month date so
        # they aren't read as amounts, then take the trailing money token.
        rest = _strip_id_dates(line.replace(datetok, " "))
        monies = _money_tokens(rest)
        if not monies:
            continue
        raw_amt, amount = monies[-1]   # trailing column
        # Description = line minus the date and money tokens.
        desc = rest
        for tok, _ in monies:
            desc = desc.replace(tok, " ")
        desc = re.sub(r"\s{2,}", " ", desc).strip(" -|\t")
        rows.append({
            "date": date,
            "description": desc or None,
            "amount": abs(amount),
            "direction": "income" if _CREDIT_RE.search(line) else "expense",
            "ambiguous": len(monies) > 1,
        })
    return rows


# --------------------------------------------------------------------------
# AI parser (opt-in)
# --------------------------------------------------------------------------

_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                    "description": {"type": "string"},
                    "amount": {"type": "integer",
                               "description": "positive IDR integer, no decimals"},
                    "direction": {"type": "string", "enum": ["expense", "income"]},
                },
                "required": ["date", "description", "amount", "direction"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["transactions"],
    "additionalProperties": False,
}


def parse_statement_ai(text: str, model: str) -> list[dict]:
    try:
        import anthropic
    except ImportError as e:
        raise SystemExit("--ai needs the anthropic SDK. Run: pip install anthropic") from e
    from dropbox_sync import load_env
    load_env()   # pick up ANTHROPIC_API_KEY from .env if present

    print(f"\n  [AI] Sending {len(text):,} chars of extracted TEXT to {model} "
          f"(the PDF file itself is NOT sent).")
    try:
        client = anthropic.Anthropic()   # resolves ANTHROPIC_API_KEY / ant profile
    except Exception as e:
        raise SystemExit(f"Could not init Anthropic client ({e}). Set "
                         "ANTHROPIC_API_KEY in .env or run `ant auth login`.") from e
    prompt = (
        "Extract every transaction from this Indonesian bank / credit-card / "
        "e-wallet statement text. Amounts are IDR — return them as positive "
        "integers with no decimals or separators. Use direction 'income' for "
        "credits/refunds/top-ups/cashback and 'expense' for purchases/debits. "
        "Ignore summary lines, balances, fees headers, and totals. Dates as "
        "ISO YYYY-MM-DD.\n\n--- STATEMENT TEXT ---\n" + text
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _AI_SCHEMA}},
        )
    except anthropic.AuthenticationError as e:
        raise SystemExit("Anthropic auth failed. Set ANTHROPIC_API_KEY in .env "
                         "or run `ant auth login`.") from e
    body = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(body)
    rows = []
    for t in data.get("transactions", []):
        date = parse_date(str(t.get("date", "")))
        amount = t.get("amount")
        if date is None or amount is None:
            continue
        rows.append({
            "date": date,
            "description": (t.get("description") or "").strip() or None,
            "amount": abs(int(amount)),
            "direction": "income" if t.get("direction") == "income" else "expense",
            "ambiguous": False,
        })
    return rows


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------

def _unlock(path: Path, batch_pw: str | None):
    """Extract text, prompting for a password if the PDF is encrypted.

    Returns (text, method, batch_pw). A password entered interactively is
    returned so it can be reused for the rest of the batch. On failure returns
    (None, None, batch_pw).
    """
    try:
        text, method = extract_text(path, batch_pw)
        return text, method, batch_pw
    except NeedPassword:
        pass

    import getpass
    print(f"  {path.name} is password-protected.")
    for _ in range(3):
        try:
            pw = getpass.getpass(f"  Password for {path.name} "
                                 "(hidden; not stored): ")
        except (EOFError, KeyboardInterrupt):
            return None, None, batch_pw
        if not pw:
            return None, None, batch_pw
        try:
            text, method = extract_text(path, pw)
            return text, method, pw     # reuse for later files in the batch
        except NeedPassword:
            print("  ! wrong password, try again.")
    return None, None, batch_pw


def ingest(paths: list[Path], db_path: Path, account: str | None,
           source_type: str | None, use_ai: bool, model: str,
           dry_run: bool, force: bool, redact: bool = True,
           password: str | None = None) -> int:
    conn = connect(db_path)
    total_inserted = 0
    # Password precedence: explicit arg -> env (.env) -> interactive prompt.
    from dropbox_sync import load_env
    load_env()
    batch_pw = password or os.getenv("PFA_PDF_PASSWORD")
    try:
        _cats, merchants_simple, accounts = load_lookups(conn)
        mapping = load_map(conn)

        account_id = None
        if account:
            account_id = accounts.get(account.lower())
            if account_id is None:
                raise SystemExit(f"Unknown account: {account!r}. "
                                 f"Known: {sorted(accounts)}")

        for path in paths:
            fhash = file_sha256(path)
            existing = conn.execute(
                "SELECT id, ingested_at FROM statements WHERE file_hash=?",
                (fhash,)).fetchone()
            if existing and not force:
                print(f"\n{path.name}: already ingested {existing[1]} "
                      f"(use --force to re-ingest). Skipping.")
                continue

            print(f"\n{path.name}")
            text, method, batch_pw = _unlock(path, batch_pw)
            if text is None:
                print("  ! could not unlock (wrong/no password), skipping.")
                continue
            print(f"  [{method}] — {len(text):,} chars extracted")

            if use_ai:
                if redact:
                    text, pii_counts = redact_text(text)
                    print(f"  [PII] redacted before AI send: "
                          f"{dict(pii_counts) or 'nothing matched'}")
                else:
                    print("  [PII] WARNING: --no-redact — raw text will be sent to Claude")
                parsed = parse_statement_ai(text, model)
            else:
                parsed = parse_statement_local(text)
            if not parsed:
                print("  ! no transactions parsed. "
                      "Try --ai, or check the extracted text.")
                continue

            ambiguous = sum(1 for p in parsed if p.get("ambiguous"))
            dates = sorted(p["date"] for p in parsed)
            print(f"  parsed {len(parsed)} transaction(s), "
                  f"{dates[0]}..{dates[-1]}"
                  + (f"  ({ambiguous} ambiguous — multiple amounts on a line)"
                     if ambiguous else ""))

            rows, uncategorized = [], 0
            for p in parsed:
                # Strip any card/account numbers from the description before it
                # is categorized or stored locally.
                merch = redact_field(p["description"])
                cat_id = (merchants_simple.get(merch.lower()) if merch else None) \
                    or match_category(merch or "", mapping)
                if cat_id is None:
                    uncategorized += 1
                rows.append((p["date"], p["amount"], p["direction"], cat_id,
                             merch, account_id, None, "statement", None))

            if uncategorized:
                print(f"  {uncategorized} uncategorized (run categorize.py later)")

            if dry_run:
                for p in parsed[:8]:
                    print(f"    {p['date']}  {p['amount']:>12,}  "
                          f"{p['direction']:<7}  {(p['description'] or '')[:40]}")
                if len(parsed) > 8:
                    print(f"    ... and {len(parsed) - 8} more")
                continue

            inserted, dups = commit_transactions(conn, rows)
            conn.execute(
                "INSERT OR IGNORE INTO statements "
                "(file_hash, file_name, source_type, account_id, "
                " period_start, period_end, row_count) VALUES (?,?,?,?,?,?,?)",
                (fhash, path.name, source_type, account_id,
                 dates[0], dates[-1], inserted),
            )
            conn.commit()
            total_inserted += inserted
            msg = f"  inserted {inserted}"
            if dups:
                msg += f", skipped {dups} duplicate(s)"
            print(msg + ".")

        if dry_run:
            print("\n[dry-run] No rows written.")
        return total_inserted
    finally:
        conn.close()


def collect_pdfs(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.iterdir() if p.suffix.lower() == ".pdf")
    if target.suffix.lower() == ".pdf":
        return [target]
    raise SystemExit(f"Not a PDF or folder of PDFs: {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDF statements.")
    parser.add_argument("target", type=Path, help="PDF file or folder of PDFs")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--account", help="Account name for these statements")
    parser.add_argument("--type", dest="source_type", choices=SOURCE_TYPES,
                        help="Statement type (cc/ewallet/bank/other)")
    parser.add_argument("--ai", action="store_true",
                        help="Opt-in: send extracted TEXT to Claude to structure rows")
    parser.add_argument("--model", default=AI_MODEL_DEFAULT,
                        help=f"AI model for --ai (default: {AI_MODEL_DEFAULT})")
    parser.add_argument("--force", action="store_true",
                        help="Re-ingest even if the file was already processed")
    parser.add_argument("--no-redact", dest="redact", action="store_false",
                        help="Do NOT redact PII before an --ai send (not recommended)")
    parser.add_argument("--password", help="PDF password (better: set PFA_PDF_PASSWORD "
                        "in .env, or leave blank to be prompted securely)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and preview without writing")
    args = parser.parse_args()

    if not args.target.exists():
        raise SystemExit(f"Path not found: {args.target}")
    pdfs = collect_pdfs(args.target)
    if not pdfs:
        raise SystemExit(f"No PDF files found in {args.target}")
    print(f"Found {len(pdfs)} PDF(s). Parser: {'AI' if args.ai else 'local'}")
    ingest(pdfs, args.db, args.account, args.source_type, args.ai,
           args.model, args.dry_run, args.force, args.redact, args.password)


if __name__ == "__main__":
    main()
