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
# Credit markers -> treat as income rather than expense.
_CREDIT_RE = re.compile(r"\b(cr|kredit|credit|masuk|refund|cashback|top ?up)\b", re.I)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_text_pdfplumber(path: Path) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def ocr_pdf(path: Path, lang: str = "ind+eng") -> str:
    """Render each page and OCR it (for scanned statements)."""
    try:
        import pypdfium2 as pdfium
        import pytesseract
    except ImportError as e:
        raise SystemExit("Scanned PDF needs pypdfium2 + pytesseract. "
                         "Run: pip install pypdfium2 pytesseract") from e
    text_parts = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            pil = page.render(scale=2.0).to_pil()   # ~144 dpi
            text_parts.append(pytesseract.image_to_string(pil, lang=lang))
    finally:
        pdf.close()
    return "\n".join(text_parts)


def extract_text(path: Path) -> tuple[str, str]:
    """Return (text, method). Falls back to OCR when the PDF has no text layer."""
    text = extract_text_pdfplumber(path)
    if len(text.strip()) >= 40:
        return text, "text"
    return ocr_pdf(path), "ocr"


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
    rows = []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        dm = _DATE_RE.search(line)
        if not dm:
            continue
        date = parse_date(dm.group(1))
        if not date:
            continue
        # Strip the date first so its year (e.g. 2026) isn't read as an amount.
        rest = line.replace(dm.group(1), " ")
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

def ingest(paths: list[Path], db_path: Path, account: str | None,
           source_type: str | None, use_ai: bool, model: str,
           dry_run: bool, force: bool, redact: bool = True) -> int:
    conn = connect(db_path)
    total_inserted = 0
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

            text, method = extract_text(path)
            print(f"\n{path.name} [{method}] — {len(text):,} chars extracted")

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
           args.model, args.dry_run, args.force, args.redact)


if __name__ == "__main__":
    main()
