#!/usr/bin/env python3
"""Ingest an expense sheet (CSV / XLSX) into the transactions table.

Deterministic, pandas-based. No AI. Reads a spreadsheet, normalizes the
columns, auto-categorizes each row via merchant_category_map, and inserts
rows into `transactions` with source='sheet'.

Usage:
    python scripts/ingest_sheet.py data/august.csv
    python scripts/ingest_sheet.py data/august.xlsx --sheet "Sheet1"
    python scripts/ingest_sheet.py data/august.csv --account "BCA Checking"
    python scripts/ingest_sheet.py data/august.csv --dry-run

Expected columns (header names are matched case-insensitively; common
Indonesian aliases are accepted):
    date       (required)  date, tanggal
    amount     (required)  amount, jumlah, nominal, total, harga
    merchant                merchant, toko, vendor, description, keterangan
    category                category, kategori
    account                 account, akun, rekening
    family_member           family_member, member, anggota
    type                    type, tipe            (expense/income/transfer)
    notes                   notes, note, catatan

Categorization:
    - If a `category` value is given and matches an existing category name,
      that wins.
    - Otherwise the merchant is looked up in merchant_category_map.
    - If neither resolves, the row is inserted with NULL category and
      reported so it can be categorized later (build step 4).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# Date formats tried in order. ISO first so 'YYYY-MM-DD' is never read as
# day-first; the remaining formats are day-first (Indonesian convention).
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")
_ISO_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")


def parse_date(raw: str) -> str | None:
    """Parse a date string into ISO 'YYYY-MM-DD', or None if unparseable."""
    raw = raw.strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Fallback: let pandas guess, day-first unless it already looks ISO.
    try:
        dt = pd.to_datetime(raw, dayfirst=not _ISO_RE.match(raw))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None

# Reuse the shared connection helper / default DB path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect  # noqa: E402

# Header alias -> canonical field name. Keys are lowercased.
COLUMN_ALIASES = {
    "date": "date", "tanggal": "date", "tgl": "date",
    "amount": "amount", "jumlah": "amount", "nominal": "amount",
    "total": "amount", "harga": "amount",
    "merchant": "merchant", "toko": "merchant", "vendor": "merchant",
    "description": "merchant", "deskripsi": "merchant", "keterangan": "merchant",
    "category": "category", "kategori": "category",
    "account": "account", "akun": "account", "rekening": "account",
    "family_member": "family_member", "member": "family_member",
    "anggota": "family_member",
    "type": "type", "tipe": "type",
    "notes": "notes", "note": "notes", "catatan": "notes",
}

VALID_TYPES = {"expense", "income", "transfer"}


def parse_amount(value) -> int | None:
    """Parse an IDR amount into an integer number of rupiah.

    Handles plain numbers as well as Indonesian-formatted strings such as
    'Rp 3.500.000', '3.500.000,00', or '350,000'.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int,)):
        return int(value)
    if isinstance(value, float):
        return int(round(value))

    s = str(value).strip()
    if not s:
        return None

    negative = s.startswith("-") or s.startswith("(")
    # Keep only digit-group separators and digits.
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".,")
    if not s:
        return None

    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # Indonesian: '.' thousands, ',' decimal -> 3.500.000,00
        s = s.replace(".", "").replace(",", ".")
    elif has_comma:
        # Comma as decimal separator (IDR convention).
        s = s.replace(",", ".")
    elif has_dot:
        # Dots as thousands separators -> drop them (IDR has no sub-rupiah).
        s = s.replace(".", "")

    try:
        amount = int(round(float(s)))
    except ValueError:
        return None
    return -amount if negative else amount


def load_lookups(conn: sqlite3.Connection):
    """Return (category_name->id, merchant_name->category_id, account_name->id)."""
    cur = conn.cursor()
    categories = {name.lower(): cid for cid, name in
                  cur.execute("SELECT id, name FROM categories")}
    merchants = {name.lower(): cid for name, cid in
                 cur.execute("SELECT merchant_name, category_id FROM merchant_category_map")}
    accounts = {name.lower(): aid for aid, name in
                cur.execute("SELECT id, name FROM accounts")}
    return categories, merchants, accounts


def read_sheet(path: Path, sheet: str | None) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path, sheet_name=sheet or 0, dtype=str,
                             keep_default_na=False)
    raise ValueError(f"Unsupported file type: {suffix} (expected .csv / .xlsx)")


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename columns to canonical field names via COLUMN_ALIASES."""
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in COLUMN_ALIASES:
            rename[col] = COLUMN_ALIASES[key]
    return df.rename(columns=rename)


def ingest(path: Path, db_path: Path, default_account: str | None,
           sheet: str | None, dry_run: bool) -> int:
    df = normalize_columns(read_sheet(path, sheet))

    missing = [c for c in ("date", "amount") if c not in df.columns]
    if missing:
        raise SystemExit(
            f"Sheet is missing required column(s): {', '.join(missing)}.\n"
            f"Found columns: {list(df.columns)}"
        )

    conn = connect(db_path)
    try:
        categories, merchants, accounts = load_lookups(conn)

        default_account_id = None
        if default_account:
            default_account_id = accounts.get(default_account.lower())
            if default_account_id is None:
                raise SystemExit(f"Unknown account: {default_account!r}. "
                                 f"Known: {sorted(a for a in accounts)}")

        rows_to_insert = []
        uncategorized: list[str] = []
        unknown_accounts: set[str] = set()
        skipped = 0

        for i, row in df.iterrows():
            line = i + 2  # +1 for header, +1 for 1-based

            raw_date = str(row.get("date", "")).strip()
            amount = parse_amount(row.get("amount"))
            if not raw_date or amount is None:
                skipped += 1
                continue

            date = parse_date(raw_date)
            if date is None:
                print(f"  ! line {line}: unparseable date {raw_date!r}, skipping")
                skipped += 1
                continue

            merchant = (str(row.get("merchant", "")).strip() or None)

            # Resolve category: explicit category column first, then merchant map.
            category_id = None
            cat_name = str(row.get("category", "")).strip()
            if cat_name:
                category_id = categories.get(cat_name.lower())
                if category_id is None:
                    print(f"  ! line {line}: unknown category {cat_name!r} "
                          f"(row kept uncategorized)")
            if category_id is None and merchant:
                category_id = merchants.get(merchant.lower())
            if category_id is None:
                uncategorized.append(merchant or f"(line {line})")

            # Resolve account: per-row column, else the --account default.
            account_id = default_account_id
            acct_name = str(row.get("account", "")).strip()
            if acct_name:
                resolved = accounts.get(acct_name.lower())
                if resolved is None:
                    unknown_accounts.add(acct_name)
                else:
                    account_id = resolved

            txn_type = str(row.get("type", "")).strip().lower() or "expense"
            if txn_type not in VALID_TYPES:
                txn_type = "expense"

            family_member = str(row.get("family_member", "")).strip() or None
            notes = str(row.get("notes", "")).strip() or None

            rows_to_insert.append((date, abs(amount), txn_type, category_id,
                                   merchant, account_id, family_member,
                                   "sheet", notes))

        # --- Report -----------------------------------------------------
        print(f"\nParsed {len(rows_to_insert)} row(s) from {path.name} "
              f"({skipped} skipped).")
        if unknown_accounts:
            print(f"  Unknown account name(s) (left NULL): "
                  f"{sorted(unknown_accounts)}")
        if uncategorized:
            from collections import Counter
            top = Counter(uncategorized).most_common()
            print(f"  Uncategorized ({len(uncategorized)}): "
                  f"{', '.join(f'{m}×{n}' if n > 1 else m for m, n in top)}")

        if dry_run:
            print("\n[dry-run] No rows written.")
            return 0

        from ingest_core import commit_transactions
        inserted, dups = commit_transactions(conn, rows_to_insert)
        msg = f"\nInserted {inserted} transaction(s) into {db_path}."
        if dups:
            msg += f" Skipped {dups} duplicate(s)."
        print(msg)
        return inserted
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a CSV/XLSX expense sheet.")
    parser.add_argument("file", type=Path, help="Path to the .csv or .xlsx file")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--account", help="Default account name for rows with no account column")
    parser.add_argument("--sheet", help="Worksheet name for XLSX (default: first)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report without writing to the DB")
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file}")

    ingest(args.file, args.db, args.account, args.sheet, args.dry_run)


if __name__ == "__main__":
    main()
