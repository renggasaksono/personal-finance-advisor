#!/usr/bin/env python3
"""Ingest receipt photos into the transactions table via OCR.

Deterministic pipeline: OCR the image to text (pluggable engine, local by
default), extract merchant / date / total from the text with regex + keyword
heuristics, auto-categorize via merchant_category_map, and insert a row with
source='receipt'.

Images are read in place and NOT copied or retained (privacy: the source of
truth stays wherever you keep the original). Only extracted fields are stored.

Usage:
    python scripts/ingest_receipt.py data/receipts/indomaret.jpg
    python scripts/ingest_receipt.py data/receipts/            # whole folder
    python scripts/ingest_receipt.py data/receipts/ --dry-run  # preview only
    python scripts/ingest_receipt.py r.jpg --engine easyocr --account Cash

Because OCR is error-prone, run --dry-run first to review the extracted
fields before writing. Rows with a total that could not be parsed are skipped.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect          # noqa: E402
from ingest_sheet import parse_amount, parse_date, load_lookups  # noqa: E402
from ocr_engines import DEFAULT_ENGINE, get_engine  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}

# Keyword weights for locating the grand total. Higher wins.
_TOTAL_KEYWORDS = [
    (re.compile(r"grand\s*total", re.I), 4),
    (re.compile(r"total\s*(bayar|belanja|akhir|harga)", re.I), 4),
    (re.compile(r"\btotal\b", re.I), 3),
    (re.compile(r"\bjumlah\b", re.I), 2),
]
# Lines that mention a number but must NOT be treated as the total.
_TOTAL_EXCLUDE = re.compile(
    r"sub\s*total|kembali|tunai|cash|change|kembalian|npwp|no\.?\s|qty|item|"
    r"ppn|pajak|discount|diskon|tax",
    re.I,
)

# Date anywhere in the text (dd/mm/yyyy, dd-mm-yy, yyyy-mm-dd, dd.mm.yyyy).
_DATE_RE = re.compile(
    r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2}"        # ISO
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b"    # day-first
)
# A currency-ish number token, e.g. 1.250.000  /  75,000  /  Rp 12.500
_MONEY_RE = re.compile(r"(?:rp\.?\s*)?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?|\d{4,})", re.I)


def _amounts_in(line: str) -> list[int]:
    out = []
    for m in _MONEY_RE.finditer(line):
        val = parse_amount(m.group(1))
        if val is not None:
            out.append(val)
    return out


def extract_merchant(lines: list[str]) -> str | None:
    """Merchant name is usually the first meaningful line at the top."""
    for line in lines[:6]:
        s = line.strip()
        if len(s) < 3:
            continue
        # Skip lines that are mostly digits/punctuation (addresses, phone, NPWP).
        letters = sum(ch.isalpha() for ch in s)
        if letters < max(3, len(s) * 0.4):
            continue
        if re.search(r"jl\.|jalan|no\.|telp|npwp", s, re.I):
            continue
        return s
    return None


def extract_date(text: str) -> str | None:
    for m in _DATE_RE.finditer(text):
        parsed = parse_date(m.group(1))
        if parsed:
            return parsed
    return None


def extract_total(lines: list[str]) -> int | None:
    """Find the grand total by keyword weight; fall back to the max amount."""
    best_score = 0
    best_amount = None
    for i, line in enumerate(lines):
        if _TOTAL_EXCLUDE.search(line):
            continue
        for pattern, weight in _TOTAL_KEYWORDS:
            if pattern.search(line):
                # Amount may be on the same line or the following one.
                amounts = _amounts_in(line) or (
                    _amounts_in(lines[i + 1]) if i + 1 < len(lines) else [])
                if amounts and weight > best_score:
                    best_score = weight
                    best_amount = max(amounts)
                break
    if best_amount is not None:
        return best_amount

    # Fallback: the largest money-like number anywhere (excluding noise lines).
    candidates = [a for i, line in enumerate(lines)
                  if not _TOTAL_EXCLUDE.search(line)
                  for a in _amounts_in(line)]
    return max(candidates) if candidates else None


def parse_receipt_text(text: str) -> dict:
    """Extract structured fields from raw OCR text."""
    lines = [ln for ln in (l.rstrip() for l in text.splitlines()) if ln.strip()]
    return {
        "merchant": extract_merchant(lines),
        "date": extract_date(text),
        "total": extract_total(lines),
    }


def categorize(merchant: str | None, text: str, merchants: dict) -> int | None:
    """Match a known merchant name inside the merchant line or full text."""
    if not merchants:
        return None
    hay = ((merchant or "") + "\n" + text).lower()
    # Longest known name first, so 'apotek k24' beats a bare 'apotek'.
    for name in sorted(merchants, key=len, reverse=True):
        if name in hay:
            return merchants[name]
    return None


def ingest(paths: list[Path], db_path: Path, engine_name: str,
           default_account: str | None, dry_run: bool) -> int:
    engine = get_engine(engine_name)
    conn = connect(db_path)
    try:
        _cats, merchants, accounts = load_lookups(conn)

        account_id = None
        if default_account:
            account_id = accounts.get(default_account.lower())
            if account_id is None:
                raise SystemExit(f"Unknown account: {default_account!r}. "
                                 f"Known: {sorted(accounts)}")

        rows, skipped = [], 0
        for path in paths:
            text = engine.extract_text(path)
            fields = parse_receipt_text(text)
            cat_id = categorize(fields["merchant"], text, merchants)

            cat_label = next((n for n, c in
                              zip(_cats.keys(), _cats.values()) if c == cat_id),
                             None) if cat_id else None
            print(f"\n{path.name}")
            print(f"  merchant : {fields['merchant']}")
            print(f"  date     : {fields['date']}")
            print(f"  total    : {fields['total']}")
            print(f"  category : {cat_label or '(uncategorized)'}")

            if fields["total"] is None:
                print("  ! no total parsed — skipping (review manually)")
                skipped += 1
                continue
            if fields["date"] is None:
                print("  ! no date parsed — skipping (review manually)")
                skipped += 1
                continue

            rows.append((fields["date"], abs(fields["total"]), "expense",
                         cat_id, fields["merchant"], account_id, None,
                         "receipt", None))

        print(f"\nParsed {len(rows)} receipt(s), {skipped} skipped.")
        if dry_run:
            print("[dry-run] No rows written.")
            return 0

        if rows:
            conn.executemany(
                "INSERT INTO transactions "
                "(date, amount, type, category_id, merchant, account_id, "
                " family_member, source, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        print(f"Inserted {len(rows)} transaction(s) into {db_path}.")
        return len(rows)
    finally:
        conn.close()


def collect_images(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.iterdir()
                      if p.suffix.lower() in IMAGE_EXTS)
    if target.suffix.lower() in IMAGE_EXTS:
        return [target]
    raise SystemExit(f"Not an image or folder of images: {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest receipt photos via OCR.")
    parser.add_argument("target", type=Path,
                        help="Receipt image file or a folder of images")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--engine", default=DEFAULT_ENGINE,
                        help=f"OCR engine (default: {DEFAULT_ENGINE})")
    parser.add_argument("--account", help="Account name to assign to these receipts")
    parser.add_argument("--dry-run", action="store_true",
                        help="OCR and preview extraction without writing")
    args = parser.parse_args()

    if not args.target.exists():
        raise SystemExit(f"Path not found: {args.target}")

    images = collect_images(args.target)
    if not images:
        raise SystemExit(f"No image files found in {args.target}")
    print(f"Found {len(images)} image(s). Engine: {args.engine}")
    ingest(images, args.db, args.engine, args.account, args.dry_run)


if __name__ == "__main__":
    main()
