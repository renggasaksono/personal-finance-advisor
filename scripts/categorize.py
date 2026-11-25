#!/usr/bin/env python3
"""Merchant categorization + merchant_category_map learning loop.

After ingestion, some spending has no category (unknown merchant). Income and
transfers are never categorized. Bank descriptions are grouped by a stable
merchant name (merchant_key), so one answer covers every row from that
merchant now and in future imports. This tool lets you:

    list     show uncategorized spending, grouped by merchant
    apply    re-apply the current merchant_category_map to uncategorized rows
             (useful after the map grows)
    set      map a merchant -> category, then backfill matching rows
    review   interactive loop: confirm a category for each unknown merchant
    stats    coverage summary

Every confirmed mapping is written to merchant_category_map with
confidence='confirmed' so future ingests categorize the merchant automatically.

Usage:
    python scripts/categorize.py list
    python scripts/categorize.py apply
    python scripts/categorize.py set "Warung Tegal Bahari" "Household"
    python scripts/categorize.py set "Tokopedia" 8            # by category id
    python scripts/categorize.py review
    python scripts/categorize.py stats
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_category(conn: sqlite3.Connection, ref: str) -> tuple[int, str]:
    """Resolve a category reference (id or name) to (id, name)."""
    ref = ref.strip()
    row = None
    if ref.isdigit():
        row = conn.execute("SELECT id, name FROM categories WHERE id=?",
                           (int(ref),)).fetchone()
    if row is None:
        row = conn.execute("SELECT id, name FROM categories WHERE name=? COLLATE NOCASE",
                           (ref,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown category: {ref!r}. Use 'categorize.py "
                         f"stats' or check the categories table.")
    return row[0], row[1]


def match_category(merchant: str, mapping: list[tuple[str, int]]) -> int | None:
    """Match a transaction merchant against map entries.

    Prefers an exact case-insensitive match, then a substring match either
    direction (map name in merchant, or merchant in map name). Longer map
    names win so 'apotek k24' beats a bare 'apotek'.
    """
    if not merchant:
        return None
    m = merchant.lower()
    # Exact first.
    for name, cid in mapping:
        if name.lower() == m:
            return cid
    # Substring, longest name first.
    for name, cid in sorted(mapping, key=lambda x: len(x[0]), reverse=True):
        n = name.lower()
        if n in m or m in n:
            return cid
    return None


def load_map(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in
            conn.execute("SELECT merchant_name, category_id FROM merchant_category_map")]


# --- Stable merchant names from bank descriptions --------------------------
# Bank exports put dates and reference numbers in the description
# ('TRANSAKSI DEBIT / TGL: 0902 QR 914 00000.00Soto Lamon'), so the raw text
# differs every month. merchant_key() cuts out the part that names who was
# paid. It is always a substring of the description, so a mapping saved under
# it matches future rows through the existing substring matching.
_KEY_RULES = [
    # Bank fees: one key per fee kind, wherever they appear.
    re.compile(r"\b(BIAYA\s+TXN|BIAYA\s+ADM|FEE\s+TRF(?:\s+QR)?)\b", re.I),
    # Cash out.
    re.compile(r"^(TARIKAN\s+ATM)\b", re.I),
    re.compile(r"\b(WITHDRAWAL)\b", re.I),
    # QR payment: 'QR 914 00000.00<merchant>'.
    re.compile(r"\bQRC?\s+\d+\s+[\d.,]+\s*(\S.*)$", re.I),
    # Debit card at a domestic merchant: 'TRN DEBIT DOM 002 <merchant>'.
    re.compile(r"\bTRN\s+DEBIT\s+DOM\s+\d+\s+(\S.*)$", re.I),
    # Transfer to/from a person: 'TRANSFER KE 008 <name>', 'TRF QR KE 915 <name>'.
    re.compile(r"\b(?:TRANSFER|TRF(?:\s+QR)?)\s+(?:KE|DR)\s+\d+\s+(\S.*)$", re.I),
    # Virtual-account bill payment: '... / 15918/ASTRO / ...'.
    re.compile(r"/FTFVA/.*? / \d+/([^/]*?[A-Za-z][^/]*?)(?: / |$)", re.I),
]
# Trailing noise after a person's or shop's name.
_KEY_TAIL = re.compile(r"\s+(?:M-BCA|MBKBKE\b.*|Kota\b.*)$", re.I)
_KEY_SKIP = re.compile(r"^\[|^[\d\s.,/:-]*$")   # masked tokens, numbers, amounts


def merchant_key(description: str | None) -> str:
    """Stable merchant name inside a (possibly bank-style) description."""
    if not description:
        return ""
    text = description.strip()
    for rule in _KEY_RULES:
        m = rule.search(text)
        if m:
            key = _KEY_TAIL.sub("", m.group(1)).strip(" /")
            if key:
                return key
    segments = [s.strip() for s in text.split(" / ") if s.strip()]
    if len(segments) == 1:
        return segments[0]
    # Otherwise the last meaningful segment names the counterparty
    # ('TRSF E-BANKING DB / ... / 25000.00 / <recipient>', 'KARTU DEBIT / <shop>').
    for seg in reversed(segments[1:]):
        if not _KEY_SKIP.search(seg):
            return _KEY_TAIL.sub("", seg).strip()
    return segments[0]


def uncategorized_merchants(conn: sqlite3.Connection):
    """Uncategorized SPENDING grouped by merchant_key, largest total first.

    Returns [(key, count, total, example_description, [ids])]. Income and
    transfers are left out: categories only describe what money was spent on.
    """
    groups: dict[str, list] = {}
    for tid, merchant, amount in conn.execute(
            "SELECT id, merchant, amount FROM transactions "
            "WHERE category_id IS NULL AND type='expense'"):
        key = merchant_key(merchant) or "(no merchant)"
        g = groups.setdefault(key.lower(), [key, 0, 0, merchant or "", []])
        g[1] += 1
        g[2] += amount
        g[4].append(tid)
    return sorted((tuple(g) for g in groups.values()), key=lambda g: -g[2])


def upsert_mapping(conn: sqlite3.Connection, merchant: str, category_id: int) -> None:
    conn.execute(
        "INSERT INTO merchant_category_map (merchant_name, category_id, confidence, last_confirmed) "
        "VALUES (?,?, 'confirmed', ?) "
        "ON CONFLICT(merchant_name) DO UPDATE SET "
        "  category_id=excluded.category_id, confidence='confirmed', "
        "  last_confirmed=excluded.last_confirmed",
        (merchant, category_id, date.today().isoformat()),
    )


def backfill(conn: sqlite3.Connection, merchant: str, category_id: int) -> int:
    """Assign category to uncategorized spending whose merchant matches (ci, substring)."""
    cur = conn.execute(
        "UPDATE transactions SET category_id=? "
        "WHERE category_id IS NULL AND type='expense' AND merchant IS NOT NULL AND ("
        "  LOWER(merchant)=LOWER(?) "
        "  OR INSTR(LOWER(merchant), LOWER(?))>0 "
        "  OR INSTR(LOWER(?), LOWER(merchant))>0)",
        (category_id, merchant, merchant, merchant),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(conn: sqlite3.Connection) -> None:
    rows = uncategorized_merchants(conn)
    if not rows:
        print("No uncategorized transactions. ")
        return
    print(f"Uncategorized spending by merchant ({len(rows)}):\n")
    print(f"  {'merchant':<32} {'count':>5} {'total (IDR)':>14}")
    print(f"  {'-'*32} {'-'*5} {'-'*14}")
    for m, n, total, _example, _ids in rows:
        print(f"  {m[:32]:<32} {n:>5} {total:>14,}")


def cmd_apply(conn: sqlite3.Connection) -> None:
    mapping = load_map(conn)
    if not mapping:
        print("merchant_category_map is empty; nothing to apply.")
        return
    rows = conn.execute(
        "SELECT id, merchant FROM transactions WHERE category_id IS NULL AND type='expense'"
    ).fetchall()
    updated = 0
    for tid, merchant in rows:
        cid = match_category(merchant or "", mapping)
        if cid is not None:
            conn.execute("UPDATE transactions SET category_id=? WHERE id=?", (cid, tid))
            updated += 1
    conn.commit()
    print(f"Applied map: categorized {updated} of {len(rows)} uncategorized row(s).")


def cmd_set(conn: sqlite3.Connection, merchant: str, category_ref: str) -> None:
    cid, cname = resolve_category(conn, category_ref)
    upsert_mapping(conn, merchant, cid)
    n = backfill(conn, merchant, cid)
    conn.commit()
    print(f"Mapped {merchant!r} -> {cname!r} (confirmed). Backfilled {n} row(s).")


def cmd_review(conn: sqlite3.Connection) -> None:
    cats = conn.execute("SELECT id, name FROM categories ORDER BY id").fetchall()
    rows = uncategorized_merchants(conn)
    if not rows:
        print("No uncategorized transactions. ")
        return

    print("Categories:")
    for cid, name in cats:
        print(f"  {cid:>2}  {name}")
    print("\nFor each merchant, enter a category id/name, 's' to skip, 'q' to quit.\n")

    for merchant, n, total, example, ids in rows:
        if merchant == "(no merchant)":
            continue
        # An earlier answer may already have covered this group (substring).
        marks = ",".join("?" * len(ids))
        left = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transactions "
                            f"WHERE id IN ({marks}) AND category_id IS NULL", ids).fetchone()
        if not left[0]:
            continue
        n, total = left
        hint = f"\n    e.g. {example[:90]}" if example and example != merchant else ""
        try:
            ans = input(f"{merchant}  ({n} txn, {total:,} IDR){hint}\n  -> ").strip()
        except EOFError:
            print("\n(no input available; exiting review)")
            break
        if ans.lower() in ("q", "quit"):
            break
        if ans.lower() in ("", "s", "skip"):
            continue
        try:
            cid, cname = resolve_category(conn, ans)
        except SystemExit as e:
            print(f"  {e}")
            continue
        upsert_mapping(conn, merchant, cid)
        count = backfill(conn, merchant, cid)
        conn.commit()
        print(f"  -> {cname} (confirmed), backfilled {count} row(s)")


def cmd_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM transactions WHERE type='expense'").fetchone()[0]
    uncat = conn.execute("SELECT COUNT(*) FROM transactions "
                         "WHERE category_id IS NULL AND type='expense'").fetchone()[0]
    mapped = conn.execute("SELECT COUNT(*) FROM merchant_category_map").fetchone()[0]
    cat = total - uncat
    pct = (cat / total * 100) if total else 0.0
    print(f"Spending rows      : {total}")
    print(f"  categorized      : {cat} ({pct:.1f}%)")
    print(f"  uncategorized    : {uncat}")
    print(f"merchant mappings  : {mapped}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merchant categorization + learning loop.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite DB path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show uncategorized transactions by merchant")
    sub.add_parser("apply", help="Re-apply merchant_category_map to uncategorized rows")
    sub.add_parser("review", help="Interactively categorize unknown merchants")
    sub.add_parser("stats", help="Categorization coverage summary")
    p_set = sub.add_parser("set", help="Map merchant -> category and backfill")
    p_set.add_argument("merchant")
    p_set.add_argument("category", help="Category name or id")

    args = parser.parse_args()
    conn = connect(args.db)
    try:
        if args.command == "list":
            cmd_list(conn)
        elif args.command == "apply":
            cmd_apply(conn)
        elif args.command == "review":
            cmd_review(conn)
        elif args.command == "stats":
            cmd_stats(conn)
        elif args.command == "set":
            cmd_set(conn, args.merchant, args.category)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
