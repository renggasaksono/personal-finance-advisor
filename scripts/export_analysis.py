#!/usr/bin/env python3
"""Export snapshots as a ready-to-paste Claude analysis prompt.

This is the ONLY step that produces data intended to leave the machine, and it
does so passively: it writes a prompt file for you to review and paste into
Claude yourself. No API keys, no automatic network egress. Only snapshot
aggregates + goal targets are included — never raw transactions, merchants,
or family-member identities.

Usage:
    python scripts/export_analysis.py                 # all snapshots (trend)
    python scripts/export_analysis.py --months 2026-07 2026-08
    python scripts/export_analysis.py --latest        # most recent month only
    python scripts/export_analysis.py --out data/analysis_prompt.md
    python scripts/export_analysis.py --stdout        # print instead of write

Paste the resulting file into Claude. The instructions ask Claude to use live
web search for current Indonesia market context (BI rate, inflation, etc.),
which requires a Claude interface with web search enabled.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect  # noqa: E402

DEFAULT_OUT = DEFAULT_DB.parent / "analysis_prompt.md"

# Columns that hold JSON text and must be decoded before re-serializing.
_JSON_COLS = {"category_totals", "budget_status", "goal_progress"}

INSTRUCTIONS = """\
# Personal Finance Analysis Request

You are acting as a financial analyst for an Indonesian household. Below are
monthly financial **snapshots** (aggregated — no raw transactions). All amounts
are in Indonesian Rupiah (IDR).

Please analyze and respond with:

1. **Trend & health check** — income vs. expense trajectory, savings-rate trend,
   net-worth direction, and debt-to-income (DTI) assessment.
2. **"Sidetracked" detection** — categories running over budget or drifting up
   month-over-month, and which goals are on/off pace for their target dates.
3. **Goal outlook** — for each goal, the monthly contribution needed to hit the
   target by its target date at the current pace, and whether that's realistic.
4. **Localized recommendations** — concrete, prioritized actions. Use **live web
   search** for current Indonesia market context and cite figures with dates:
     - Bank Indonesia (BI) benchmark rate and recent inflation (CPI)
     - Reksadana / SBN (e.g. ORI, SR) / deposito rates and equity climate
     - Any relevant cost-of-living or policy changes
   Frame emergency-fund and investment advice against these current conditions.
5. **Caveat** — note that this is general analysis, not licensed financial advice.

`income_basis` says where each month's income figure comes from: `actual` is
income recorded in the ledger; `baseline` is the household's stated typical
monthly income, used when no income was recorded — treat those months' savings
rate as an estimate.

Keep advice practical for a dual-income family of four. Where a recommendation
depends on data not in the snapshots, say what additional info would help.

---

## Snapshots
"""


def fetch_snapshots(conn: sqlite3.Connection, months: list[str] | None,
                    latest: bool) -> list[dict]:
    q = "SELECT month, total_income, income_basis, total_expense, total_savings, savings_rate, " \
        "net_worth, dti_ratio, category_totals, budget_status, goal_progress " \
        "FROM snapshots"
    params: tuple = ()
    if months:
        placeholders = ",".join("?" for _ in months)
        q += f" WHERE month IN ({placeholders})"
        params = tuple(months)
    q += " ORDER BY month"
    if latest:
        q += " DESC LIMIT 1"

    cols = ["month", "total_income", "income_basis", "total_expense", "total_savings",
            "savings_rate", "net_worth", "dti_ratio", "category_totals",
            "budget_status", "goal_progress"]
    out = []
    for row in conn.execute(q, params):
        rec = dict(zip(cols, row))
        for col in _JSON_COLS:
            rec[col] = json.loads(rec[col]) if rec[col] else {}
        out.append(rec)
    if latest:
        out.reverse()  # restore chronological order
    return out


def build_prompt(snapshots: list[dict]) -> str:
    payload = {
        "generated_on": date.today().isoformat(),
        "currency": "IDR",
        "household": "dual-income family of four (Indonesia)",
        "snapshots": snapshots,
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    return f"{INSTRUCTIONS}\n```json\n{body}\n```\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a Claude analysis prompt.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--months", nargs="+", metavar="YYYY-MM",
                        help="Specific months to include (default: all)")
    parser.add_argument("--latest", action="store_true",
                        help="Only the most recent snapshot")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output file (default: {DEFAULT_OUT})")
    parser.add_argument("--stdout", action="store_true",
                        help="Print to stdout instead of writing a file")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        snapshots = fetch_snapshots(conn, args.months, args.latest)
    finally:
        conn.close()

    if not snapshots:
        raise SystemExit("No snapshots found. Run generate_snapshot.py first.")

    prompt = build_prompt(snapshots)
    included = ", ".join(s["month"] for s in snapshots)

    if args.stdout:
        print(prompt)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(prompt, encoding="utf-8")
        print(f"Wrote analysis prompt to {args.out}")

    print(f"\nIncluded {len(snapshots)} snapshot(s): {included}")
    print("Contents: aggregated snapshots + goal targets only "
          "(no raw transactions / merchants / member identities).")
    print("Review the file, then paste it into Claude (with web search enabled).")


if __name__ == "__main__":
    main()
