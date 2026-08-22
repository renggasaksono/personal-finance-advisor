#!/usr/bin/env python3
"""Merchant categorization + merchant_category_map learning loop.

After ingestion, some transactions have no category (unknown merchant). This
tool lets you:

    list     show uncategorized transactions, grouped by merchant
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


def uncategorized_merchants(conn: sqlite3.Connection):
    """Distinct merchants among uncategorized transactions, with counts/totals."""
    return conn.execute(
        "SELECT COALESCE(merchant, '(no merchant)') AS m, "
        "       COUNT(*) AS n, SUM(amount) AS total "
        "FROM transactions WHERE category_id IS NULL "
        "GROUP BY m ORDER BY total DESC"
    ).fetchall()


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
    """Assign category to uncategorized rows whose merchant matches (ci, substring)."""
    cur = conn.execute(
        "UPDATE transactions SET category_id=? "
        "WHERE category_id IS NULL AND merchant IS NOT NULL AND ("
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
    print(f"Uncategorized merchants ({len(rows)}):\n")
    print(f"  {'merchant':<32} {'count':>5} {'total (IDR)':>14}")
    print(f"  {'-'*32} {'-'*5} {'-'*14}")
    for m, n, total in rows:
        print(f"  {m[:32]:<32} {n:>5} {total:>14,}")


def cmd_apply(conn: sqlite3.Connection) -> None:
    mapping = load_map(conn)
    if not mapping:
        print("merchant_category_map is empty; nothing to apply.")
        return
    rows = conn.execute(
        "SELECT id, merchant FROM transactions WHERE category_id IS NULL"
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

    for merchant, n, total in rows:
        if merchant == "(no merchant)":
            continue
        try:
            ans = input(f"{merchant}  ({n} txn, {total:,} IDR) -> ").strip()
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
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    uncat = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0]
    mapped = conn.execute("SELECT COUNT(*) FROM merchant_category_map").fetchone()[0]
    cat = total - uncat
    pct = (cat / total * 100) if total else 0.0
    print(f"Transactions       : {total}")
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
