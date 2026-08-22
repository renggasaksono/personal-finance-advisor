#!/usr/bin/env python3
"""Generate monthly snapshots — the ONLY data ever sent to Claude.

Rolls up the local ledger into the `snapshots` table: income, expense, savings
rate, per-category totals, budget status, goal progress, net worth, and DTI.
Everything is computed deterministically in Python/SQL. No AI here.

PRIVACY: snapshots contain aggregates and goal targets only — no per-
transaction detail, no merchant names, no family-member identities. Raw
transactions / family_profile / investments / assets / debts never leave the
machine. Use --show to see exactly what a snapshot contains before sharing.

Usage:
    python scripts/generate_snapshot.py 2026-08          # one month
    python scripts/generate_snapshot.py --all            # every month with data
    python scripts/generate_snapshot.py 2026-08 --show   # print the snapshot JSON
    python scripts/generate_snapshot.py 2026-08 --show --dry-run   # don't write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect  # noqa: E402


def monthly_income(conn: sqlite3.Connection) -> int:
    """Income baseline from family_profile.monthly_income_total (IDR)."""
    row = conn.execute(
        "SELECT value FROM family_profile WHERE key='monthly_income_total'"
    ).fetchone()
    if not row:
        return 0
    try:
        return int(float(row[0]))
    except (ValueError, TypeError):
        return 0


def category_totals(conn: sqlite3.Connection, month: str) -> dict[str, int]:
    """Expense totals per category name for the given month (YYYY-MM)."""
    rows = conn.execute(
        "SELECT COALESCE(c.name, '(uncategorized)') AS name, SUM(t.amount) "
        "FROM transactions t LEFT JOIN categories c ON t.category_id=c.id "
        "WHERE t.type='expense' AND substr(t.date,1,7)=? "
        "GROUP BY name ORDER BY SUM(t.amount) DESC",
        (month,),
    ).fetchall()
    return {name: int(total) for name, total in rows}


def budget_status(conn: sqlite3.Connection, month: str,
                  cat_totals: dict[str, int]) -> dict[str, dict]:
    """{category_name: {actual, limit, over}} for categories with a budget."""
    rows = conn.execute(
        "SELECT c.name, b.monthly_limit FROM budgets b "
        "JOIN categories c ON b.category_id=c.id WHERE b.month=?",
        (month,),
    ).fetchall()
    status = {}
    for name, limit in rows:
        actual = cat_totals.get(name, 0)
        status[name] = {"actual": actual, "limit": int(limit),
                        "over": actual > limit}
    return status


def goal_progress(conn: sqlite3.Connection, month: str) -> dict[str, dict]:
    """Cumulative contribution per goal up to and including `month`."""
    rows = conn.execute(
        "SELECT g.name, g.target_amount, g.target_date, "
        "       COALESCE(SUM(CASE WHEN substr(gc.date,1,7) <= ? "
        "                         THEN gc.amount END), 0) AS current "
        "FROM goals g LEFT JOIN goal_contributions gc ON gc.goal_id=g.id "
        "GROUP BY g.id ORDER BY g.priority, g.id",
        (month,),
    ).fetchall()
    out = {}
    for name, target, target_date, current in rows:
        current = int(current)
        target = int(target)
        out[name] = {
            "current": current,
            "target": target,
            "pct": round(current / target, 4) if target else None,
            "target_date": target_date,
        }
    return out


def net_worth(conn: sqlite3.Connection, month: str) -> int:
    """Net worth as of month-end (see docs/PLANNING.md formula).

    accounts (starting_balance +/- transactions up to month end)
    + latest investment valuation per holding
    + current asset estimates
    - outstanding debt balances
    """
    # Account balances: starting + income - expense (transfers net to zero).
    start = conn.execute("SELECT COALESCE(SUM(starting_balance),0) FROM accounts").fetchone()[0]
    delta = 0
    for typ, amount in conn.execute(
        "SELECT type, amount FROM transactions "
        "WHERE account_id IS NOT NULL AND substr(date,1,7) <= ?", (month,)
    ):
        if typ == "expense":
            delta -= amount
        elif typ == "income":
            delta += amount
    accounts_total = start + delta

    # Latest valuation per investment, as of month end.
    latest: dict[int, tuple[str, int]] = {}
    for inv_id, d, mv in conn.execute(
        "SELECT investment_id, date, market_value FROM investment_valuations "
        "WHERE substr(date,1,7) <= ? ORDER BY date", (month,)
    ):
        latest[inv_id] = (d, mv)  # later dates overwrite (query is ordered)
    investments_total = sum(mv for _, mv in latest.values())

    assets_total = conn.execute(
        "SELECT COALESCE(SUM(current_est_value),0) FROM assets").fetchone()[0]
    debts_total = conn.execute(
        "SELECT COALESCE(SUM(current_balance),0) FROM debts").fetchone()[0]

    return int(accounts_total + investments_total + assets_total - debts_total)


def dti_ratio(conn: sqlite3.Connection, income: int) -> float | None:
    """Debt-to-income = sum(monthly_payment) / monthly_income."""
    if not income:
        return None
    payments = conn.execute(
        "SELECT COALESCE(SUM(monthly_payment),0) FROM debts").fetchone()[0]
    return round(payments / income, 4)


def build_snapshot(conn: sqlite3.Connection, month: str) -> dict:
    income = monthly_income(conn)
    cat_totals = category_totals(conn, month)
    expense = sum(cat_totals.values())
    savings = income - expense
    return {
        "month": month,
        "total_income": income,
        "total_expense": expense,
        "total_savings": savings,
        "savings_rate": round(savings / income, 4) if income else None,
        "net_worth": net_worth(conn, month),
        "dti_ratio": dti_ratio(conn, income),
        "category_totals": cat_totals,
        "budget_status": budget_status(conn, month, cat_totals),
        "goal_progress": goal_progress(conn, month),
    }


def upsert_snapshot(conn: sqlite3.Connection, snap: dict) -> None:
    conn.execute(
        "INSERT INTO snapshots "
        "(month, total_income, total_expense, total_savings, savings_rate, "
        " net_worth, dti_ratio, category_totals, budget_status, goal_progress) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(month) DO UPDATE SET "
        "  total_income=excluded.total_income, total_expense=excluded.total_expense, "
        "  total_savings=excluded.total_savings, savings_rate=excluded.savings_rate, "
        "  net_worth=excluded.net_worth, dti_ratio=excluded.dti_ratio, "
        "  category_totals=excluded.category_totals, "
        "  budget_status=excluded.budget_status, "
        "  goal_progress=excluded.goal_progress, "
        "  created_at=datetime('now')",
        (
            snap["month"], snap["total_income"], snap["total_expense"],
            snap["total_savings"], snap["savings_rate"], snap["net_worth"],
            snap["dti_ratio"], json.dumps(snap["category_totals"]),
            json.dumps(snap["budget_status"]), json.dumps(snap["goal_progress"]),
        ),
    )


def months_with_data(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) AS m FROM transactions ORDER BY m")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate monthly snapshots.")
    parser.add_argument("month", nargs="?", help="Month as YYYY-MM")
    parser.add_argument("--all", action="store_true",
                        help="Generate for every month present in transactions")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--show", action="store_true",
                        help="Print the snapshot JSON (what gets sent to Claude)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and show without writing to snapshots")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        if args.all:
            targets = months_with_data(conn)
            if not targets:
                raise SystemExit("No transactions found; nothing to snapshot.")
        elif args.month:
            if len(args.month) != 7 or args.month[4] != "-":
                raise SystemExit(f"Bad month format: {args.month!r} (want YYYY-MM)")
            targets = [args.month]
        else:
            raise SystemExit("Provide a month (YYYY-MM) or --all.")

        for month in targets:
            snap = build_snapshot(conn, month)
            if not args.dry_run:
                upsert_snapshot(conn, snap)
            action = "computed" if args.dry_run else "wrote"
            rate = snap["savings_rate"]
            rate_str = f"{rate*100:.1f}%" if rate is not None else "n/a"
            print(f"{action} snapshot {month}: "
                  f"income={snap['total_income']:,} "
                  f"expense={snap['total_expense']:,} "
                  f"savings_rate={rate_str} "
                  f"net_worth={snap['net_worth']:,}")
            if args.show:
                print(json.dumps(snap, indent=2, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
