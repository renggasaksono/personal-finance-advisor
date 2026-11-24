#!/usr/bin/env python3
"""Reconcile likely-duplicate transactions across sources.

Exact duplicates are already blocked at ingest time by the dedup hash. This
tool catches FUZZY near-duplicates the hash can't — e.g. a receipt you snapped
AND the same charge appearing later on a credit-card statement, where the
merchant text differs. It only reports; you decide what to drop.

Match rule: same absolute amount, dates within --days of each other, and not
already the identical row. Same-source pairs on the same day are flagged too
(possible double entry) but you can restrict to cross-source with --cross-only.

Usage:
    python scripts/reconcile.py                 # list near-duplicate pairs
    python scripts/reconcile.py --days 5
    python scripts/reconcile.py --cross-only    # only across different sources
    python scripts/reconcile.py --drop 42       # delete one transaction by id
    python scripts/reconcile.py --mark-transfer 42 57
        # re-type rows as transfers, e.g. a card bill payment imported before
        # transfers were detected (shows as an expense on the bank statement
        # AND income on the card statement — same amount, a few days apart)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect  # noqa: E402


def find_pairs(conn: sqlite3.Connection, days: int, cross_only: bool):
    cross = "AND t1.source <> t2.source" if cross_only else ""
    return conn.execute(f"""
        SELECT t1.id, t1.date, t1.amount, t1.source, t1.merchant,
               t2.id, t2.date, t2.source, t2.merchant
        FROM transactions t1
        JOIN transactions t2
          ON t1.id < t2.id
         AND t1.amount = t2.amount
         AND ABS(julianday(t1.date) - julianday(t2.date)) <= ?
         {cross}
        ORDER BY t1.amount DESC, t1.date
    """, (days,)).fetchall()


def cmd_list(conn, days, cross_only) -> None:
    pairs = find_pairs(conn, days, cross_only)
    if not pairs:
        print(f"No near-duplicate pairs found (within {days} day(s)"
              + (", cross-source only)." if cross_only else ")."))
        return
    print(f"{len(pairs)} possible duplicate pair(s) within {days} day(s):\n")
    for a_id, a_date, amt, a_src, a_m, b_id, b_date, b_src, b_m in pairs:
        print(f"  {amt:>12,} IDR")
        print(f"    [id {a_id}] {a_date}  {a_src:<9} {(a_m or '')[:38]}")
        print(f"    [id {b_id}] {b_date}  {b_src:<9} {(b_m or '')[:38]}")
    print("\nReview, then remove one with:  python scripts/reconcile.py --drop <id>")


def cmd_drop(conn, tid: int) -> None:
    row = conn.execute(
        "SELECT date, amount, type, source, merchant FROM transactions WHERE id=?",
        (tid,)).fetchone()
    if not row:
        raise SystemExit(f"No transaction with id {tid}.")
    print(f"Deleting id {tid}: {row[0]}  {row[1]:,} IDR  {row[2]}  "
          f"{row[3]}  {row[4] or ''}")
    conn.execute("DELETE FROM transactions WHERE id=?", (tid,))
    conn.commit()
    print("Deleted.")


def cmd_mark_transfer(conn, ids: list[int]) -> None:
    for tid in ids:
        row = conn.execute("SELECT date, amount, type, merchant FROM transactions "
                           "WHERE id=?", (tid,)).fetchone()
        if not row:
            raise SystemExit(f"No transaction with id {tid}.")
        conn.execute("UPDATE transactions SET type='transfer' WHERE id=?", (tid,))
        print(f"id {tid}: {row[0]}  {row[1]:,} IDR  {row[2]} -> transfer  {row[3] or ''}")
    conn.commit()
    print("Re-run generate_snapshot.py to refresh affected months.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile duplicate transactions.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--days", type=int, default=3,
                        help="Max date gap for a match (default: 3)")
    parser.add_argument("--cross-only", action="store_true",
                        help="Only pairs from different sources")
    parser.add_argument("--drop", type=int, metavar="ID",
                        help="Delete one transaction by id (after review)")
    parser.add_argument("--mark-transfer", type=int, nargs="+", metavar="ID",
                        help="Re-type transactions as transfers (own-account moves)")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        if args.drop is not None:
            cmd_drop(conn, args.drop)
        elif args.mark_transfer:
            cmd_mark_transfer(conn, args.mark_transfer)
        else:
            cmd_list(conn, args.days, args.cross_only)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
