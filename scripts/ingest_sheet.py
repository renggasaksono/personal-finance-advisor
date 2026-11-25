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
from classify import is_transfer, load_context  # noqa: E402
from pii import redact_field  # noqa: E402

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
    "balance": "balance", "saldo": "balance",
    "db/cr": "direction", "d/k": "direction", "dk": "direction",
    "debit/credit": "direction", "debit/kredit": "direction",
}

# Values of a debit/credit marker column -> transaction type. BCA's CSV export
# leaves this column's header blank, so it is also detected by its values.
_DIRECTION_VALUES = {"db": "expense", "d": "expense", "debit": "expense",
                     "cr": "income", "k": "income", "kr": "income",
                     "credit": "income", "kredit": "income"}

VALID_TYPES = {"expense", "income", "transfer"}


def _normalize_separators(s: str) -> str:
    """Turn a digits-and-separators string into a float() literal.

    - Both '.' and ',' present: whichever comes LAST is the decimal point
      ('3.500.000,00' Indonesian, '1,000,000.00' English).
    - One kind only: it groups thousands if it repeats or is followed by
      exactly 3 digits ('3.500.000', '350,000', '1.250'); otherwise it is the
      decimal point ('12,5', '5000.00'). IDR has no 3-digit decimals, so a
      3-digit tail is always thousands.
    """
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        dec, grp = (",", ".") if s.rfind(",") > s.rfind(".") else (".", ",")
        return s.replace(grp, "").replace(dec, ".")
    sep = "." if has_dot else "," if has_comma else None
    if sep is None:
        return s
    if s.count(sep) > 1 or len(s) - s.rfind(sep) - 1 == 3:
        return s.replace(sep, "")
    return s.replace(sep, ".")


def parse_amount(value) -> int | None:
    """Parse an IDR amount into an integer number of rupiah.

    Handles plain numbers and both separator conventions: Indonesian
    ('Rp 3.500.000', '3.500.000,00') and English, as in BCA's CSV export
    ('1,000,000.00', '350,000'). See _normalize_separators for the rule.
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

    s = _normalize_separators(s)
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
    """Rename columns to canonical field names via COLUMN_ALIASES, and find an
    unlabelled debit/credit marker column (all values DB/CR) by its contents."""
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in COLUMN_ALIASES:
            rename[col] = COLUMN_ALIASES[key]
    if "direction" not in rename.values():
        for col in df.columns:
            if col in rename:
                continue
            vals = {str(v).strip().lower() for v in df[col]} - {""}
            if vals and vals <= _DIRECTION_VALUES.keys():
                rename[col] = "direction"
                break
    return df.rename(columns=rename)


def _clean_description(raw) -> str | None:
    """One line per description ('TRSF E-BANKING DB / .../GOPAY TOPUP'), with
    card/account numbers, phones and emails masked before it is stored."""
    parts = [re.sub(r"\s+", " ", p).strip() for p in str(raw or "").splitlines()]
    text = " / ".join(p for p in parts if p)
    return redact_field(text) or None


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
        from categorize import load_map, match_category
        mapping = load_map(conn)
        ctx = load_context(conn)
        # A debit/credit column means a bank export: apply bank-side rules.
        source_type = "bank" if "direction" in df.columns else None
        opening = None   # (date, balance before the first row), if derivable

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

            merchant = _clean_description(row.get("merchant", ""))

            # Resolve category: explicit category column first, then merchant map.
            category_id = None
            cat_name = str(row.get("category", "")).strip()
            if cat_name:
                category_id = categories.get(cat_name.lower())
                if category_id is None:
                    print(f"  ! line {line}: unknown category {cat_name!r} "
                          f"(row kept uncategorized)")
            if category_id is None and merchant:
                category_id = (merchants.get(merchant.lower())
                               or match_category(merchant, mapping))

            # Resolve account: per-row column, else the --account default.
            account_id = default_account_id
            acct_name = str(row.get("account", "")).strip()
            if acct_name:
                resolved = accounts.get(acct_name.lower())
                if resolved is None:
                    unknown_accounts.add(acct_name)
                else:
                    account_id = resolved

            # Type: an explicit type column wins; else the DB/CR marker; then
            # own-account moves (card payments, tracked top-ups) -> transfer.
            explicit = str(row.get("type", "")).strip().lower()
            marker = _DIRECTION_VALUES.get(str(row.get("direction", "")).strip().lower())
            if explicit in VALID_TYPES:
                txn_type = explicit
            else:
                txn_type = marker or "expense"
                if is_transfer(merchant or "", source_type, ctx):
                    txn_type = "transfer"
            if category_id is None and txn_type == "expense":
                uncategorized.append(merchant or f"(line {line})")

            if opening is None and marker and "balance" in df.columns:
                bal = parse_amount(row.get("balance"))
                if bal is not None:
                    sign = 1 if marker == "expense" else -1
                    opening = (date, bal + sign * abs(amount))

            family_member = str(row.get("family_member", "")).strip() or None
            notes = str(row.get("notes", "")).strip() or None

            rows_to_insert.append((date, abs(amount), txn_type, category_id,
                                   merchant, account_id, family_member,
                                   "sheet", notes))

        # --- Report -----------------------------------------------------
        print(f"\nParsed {len(rows_to_insert)} row(s) from {path.name} "
              f"({skipped} skipped).")
        if rows_to_insert:
            dates = sorted(r[0] for r in rows_to_insert)
            print(f"  {dates[0]}..{dates[-1]}")
            for t in ("income", "expense", "transfer"):
                sel = [r for r in rows_to_insert if r[2] == t]
                if sel:
                    print(f"  {t:<8} {len(sel):>4} row(s)  {sum(r[1] for r in sel):>15,}")
        if opening:
            print(f"  Opening balance before {opening[0]}: {opening[1]:,}"
                  + (f"  (set it with: setup.py account set {default_account!r} "
                     f"--balance {opening[1]})" if default_account else ""))
        if unknown_accounts:
            print(f"  Unknown account name(s) (left NULL): "
                  f"{sorted(unknown_accounts)}")
        if uncategorized:
            from collections import Counter
            top = Counter(m[:40] for m in uncategorized).most_common(8)
            print(f"  Uncategorized expenses: {len(uncategorized)} "
                  f"(run categorize.py review after importing). Most frequent:")
            for m, n in top:
                print(f"    {n:>3} x {m}")

        if dry_run:
            transfers = [r for r in rows_to_insert if r[2] == "transfer"]
            if transfers:
                print("  Transfers (own-account moves, not income or spending):")
                for r in transfers:
                    print(f"    {r[0]}  {r[1]:>13,}  {(r[4] or '')[:60]}")
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
