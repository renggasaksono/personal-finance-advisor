#!/usr/bin/env python3
"""Initialize the Personal Finance Advisor SQLite database.

Creates the full schema (cash flow, goals/family, net worth, reporting) and,
optionally, loads the sample data from docs/finance_db_schema.md.

Usage:
    python scripts/init_db.py                 # create schema if missing
    python scripts/init_db.py --seed          # also load sample data
    python scripts/init_db.py --reset         # drop everything, recreate
    python scripts/init_db.py --reset --seed  # fresh DB with sample data
    python scripts/init_db.py --db data/finance.db

The DB file is git-ignored (*.db) — it is the local source of truth and never
leaves the machine. See docs/PLANNING.md for the privacy model.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

# Default DB location, relative to the repo root (parent of scripts/).
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "finance.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# All amounts are IDR integers (no sub-rupiah). Dates are ISO text (YYYY-MM-DD).
# Foreign keys are enforced (PRAGMA foreign_keys = ON per connection).

SCHEMA = """
-- ===== Core cash flow ===================================================

-- Labels only; not live-linked to any bank / e-wallet.
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL UNIQUE,
    type             TEXT    NOT NULL CHECK (type IN ('bank', 'cash', 'ewallet', 'other')),
    starting_balance INTEGER NOT NULL DEFAULT 0,
    currency         TEXT    NOT NULL DEFAULT 'IDR'
);

-- Fixed / variable / irregular, with optional parent (self-reference).
CREATE TABLE IF NOT EXISTS categories (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL UNIQUE,
    type      TEXT    NOT NULL CHECK (type IN ('fixed', 'variable', 'irregular')),
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL
);

-- Learned merchant -> category mapping, built up over time.
CREATE TABLE IF NOT EXISTS merchant_category_map (
    id             INTEGER PRIMARY KEY,
    merchant_name  TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    category_id    INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    confidence     TEXT    NOT NULL DEFAULT 'learned'
                           CHECK (confidence IN ('confirmed', 'learned', 'guessed')),
    last_confirmed TEXT
);

-- Raw ledger. Local only, never transmitted.
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    date          TEXT    NOT NULL,
    amount        INTEGER NOT NULL,
    type          TEXT    NOT NULL DEFAULT 'expense'
                          CHECK (type IN ('expense', 'income', 'transfer')),
    category_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    merchant      TEXT,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    family_member TEXT,
    source        TEXT    NOT NULL DEFAULT 'manual'
                          CHECK (source IN ('manual', 'sheet', 'receipt', 'statement')),
    notes         TEXT,
    external_ref  TEXT,                               -- bank/statement txn id, if any
    dedup_hash    TEXT                                -- content hash for dedup
);
CREATE INDEX IF NOT EXISTS idx_transactions_date     ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_dedup
    ON transactions(dedup_hash) WHERE dedup_hash IS NOT NULL;

-- Monthly limit per category, used for "sidetracked" alerts.
CREATE TABLE IF NOT EXISTS budgets (
    id            INTEGER PRIMARY KEY,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    month         TEXT    NOT NULL,               -- 'YYYY-MM'
    monthly_limit INTEGER NOT NULL,
    UNIQUE (category_id, month)
);

-- ===== Goals & family ===================================================

-- Small key/value reference table (household_size, monthly_income_total, ...).
CREATE TABLE IF NOT EXISTS family_profile (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    target_amount INTEGER NOT NULL,
    target_date   TEXT,
    priority      TEXT    NOT NULL DEFAULT 'Medium'
                          CHECK (priority IN ('High', 'Medium', 'Low'))
    -- current_amount is derived: SUM(goal_contributions.amount) for this goal.
);

CREATE TABLE IF NOT EXISTS goal_contributions (
    id      INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    date    TEXT    NOT NULL,
    amount  INTEGER NOT NULL,
    source  TEXT,
    notes   TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_contributions_goal ON goal_contributions(goal_id);

-- ===== Net worth ========================================================

-- Holdings: reksadana, SBN, saham, emas, etc.
CREATE TABLE IF NOT EXISTS investments (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    type       TEXT    NOT NULL,          -- reksadana / sbn / saham / emas / deposito / other
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    notes      TEXT
);

-- Buy / sell / dividend / coupon history.
CREATE TABLE IF NOT EXISTS investment_transactions (
    id             INTEGER PRIMARY KEY,
    investment_id  INTEGER NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
    date           TEXT    NOT NULL,
    txn_type       TEXT    NOT NULL CHECK (txn_type IN ('buy', 'sell', 'dividend', 'coupon')),
    units          REAL,
    price_per_unit REAL,
    amount         INTEGER NOT NULL,      -- total IDR value of the transaction
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_txn_investment ON investment_transactions(investment_id);

-- Periodic price snapshots (manual or scraped).
CREATE TABLE IF NOT EXISTS investment_valuations (
    id             INTEGER PRIMARY KEY,
    investment_id  INTEGER NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
    date           TEXT    NOT NULL,
    market_value   INTEGER NOT NULL,      -- total IDR value of the holding on `date`
    price_per_unit REAL,
    UNIQUE (investment_id, date)
);
CREATE INDEX IF NOT EXISTS idx_inv_val_investment ON investment_valuations(investment_id);

-- Non-investment assets: property, vehicle, valuables.
CREATE TABLE IF NOT EXISTS assets (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL,
    type              TEXT    NOT NULL,   -- property / vehicle / valuable / other
    acquired_value    INTEGER,
    acquired_date     TEXT,
    current_est_value INTEGER NOT NULL,
    valuation_date    TEXT,
    notes             TEXT
);

-- Liabilities.
CREATE TABLE IF NOT EXISTS debts (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    principal       INTEGER,
    current_balance INTEGER NOT NULL,
    interest_rate   REAL,                 -- annual %, e.g. 6.5
    monthly_payment INTEGER,
    linked_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    start_date      TEXT,
    notes           TEXT
);

-- Ingested statement files, tracked by content hash so re-importing the same
-- file is a no-op. One row per PDF/CSV statement processed.
CREATE TABLE IF NOT EXISTS statements (
    id           INTEGER PRIMARY KEY,
    file_hash    TEXT    NOT NULL UNIQUE,   -- sha256 of the file bytes
    file_name    TEXT,
    source_type  TEXT,                       -- cc / ewallet / bank / other
    account_id   INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    period_start TEXT,
    period_end   TEXT,
    row_count    INTEGER,
    ingested_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ===== Reporting ========================================================

-- Monthly rollup. THIS is the only table whose contents get sent to Claude.
-- Flexible per-category / per-goal breakdowns are stored as JSON text.
CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY,
    month            TEXT    NOT NULL UNIQUE,   -- 'YYYY-MM'
    total_income     INTEGER NOT NULL,
    total_expense    INTEGER NOT NULL,
    total_savings    INTEGER NOT NULL,
    savings_rate     REAL,                      -- fraction 0..1
    net_worth        INTEGER,
    dti_ratio        REAL,
    category_totals  TEXT,                       -- JSON: {category_name: amount}
    budget_status    TEXT,                       -- JSON: {category_name: {actual, limit}}
    goal_progress    TEXT,                       -- JSON: {goal_name: {current, target}}
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# Tables in dependency-safe drop order (children before parents).
_DROP_ORDER = [
    "snapshots",
    "statements",
    "debts",
    "assets",
    "investment_valuations",
    "investment_transactions",
    "investments",
    "goal_contributions",
    "goals",
    "family_profile",
    "budgets",
    "transactions",
    "merchant_category_map",
    "categories",
    "accounts",
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def drop_all(conn: sqlite3.Connection) -> None:
    for table in _DROP_ORDER:
        conn.execute(f"DROP TABLE IF EXISTS {table};")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Sample data (from docs/finance_db_schema.md)
# ---------------------------------------------------------------------------

def seed(conn: sqlite3.Connection) -> None:
    c = conn.cursor()

    c.executemany(
        "INSERT INTO accounts (id, name, type, starting_balance, currency) VALUES (?,?,?,?,?)",
        [
            (1, "BCA Checking", "bank", 15_000_000, "IDR"),
            (2, "Cash", "cash", 500_000, "IDR"),
            (3, "GoPay", "ewallet", 750_000, "IDR"),
            (4, "OVO", "ewallet", 300_000, "IDR"),
        ],
    )

    # (id, name, type, parent_name) -> resolve parent_id after insert.
    categories = [
        (1, "Household", "variable", None),
        (2, "Groceries", "variable", "Household"),
        (3, "Utilities (PLN/PDAM/Internet)", "fixed", "Household"),
        (4, "Cicilan (Loan/Mortgage)", "fixed", None),
        (5, "School Fees", "fixed", None),
        (6, "Transport", "variable", None),
        (7, "Health/Medical", "irregular", None),
        (8, "Entertainment", "variable", None),
        (9, "Insurance Premium", "fixed", None),
        (10, "Gifts/Donations", "irregular", None),
    ]
    name_to_id = {name: cid for cid, name, _, _ in categories}
    c.executemany(
        "INSERT INTO categories (id, name, type, parent_id) VALUES (?,?,?,?)",
        [(cid, name, typ, name_to_id.get(parent)) for cid, name, typ, parent in categories],
    )

    c.executemany(
        "INSERT INTO merchant_category_map (id, merchant_name, category_id, confidence, last_confirmed) VALUES (?,?,?,?,?)",
        [
            (1, "Indomaret", 2, "confirmed", "2026-08-10"),
            (2, "PLN", 3, "confirmed", "2026-08-01"),
            (3, "Grab", 6, "confirmed", "2026-08-15"),
            (4, "Sekolah Harapan Bangsa", 5, "confirmed", "2026-08-01"),
            (5, "Apotek K24", 7, "confirmed", "2026-08-12"),
        ],
    )

    c.executemany(
        "INSERT INTO transactions (id, date, amount, type, category_id, merchant, account_id, family_member, source, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "2026-08-01", 3_500_000, "expense", 4, "Bank Mandiri", 1, None, "manual", "Cicilan rumah"),
            (2, "2026-08-02", 850_000, "expense", 2, "Indomaret", 3, "Istri", "receipt", "Belanja mingguan"),
            (3, "2026-08-03", 45_000, "expense", 6, "Grab", 4, None, "manual", "Transport kantor"),
            (4, "2026-08-05", 2_000_000, "expense", 5, "Sekolah Harapan Bangsa", 1, "Anak 1", "sheet", "SPP Agustus"),
            (5, "2026-08-07", 120_000, "expense", 7, "Apotek K24", 2, "Anak 2", "receipt", "Obat batuk"),
            (6, "2026-08-10", 350_000, "expense", 8, "Cinema XXI", 3, "Keluarga", "manual", "Nonton bareng"),
        ],
    )

    c.executemany(
        "INSERT INTO family_profile (key, value) VALUES (?,?)",
        [
            ("household_size", "4"),
            ("members", "Saya (income), Istri (income), Anak 1 (8th grade), Anak 2 (5th grade)"),
            ("monthly_income_total", "25000000"),
            ("income_sources",
             "Gaji Saya: 15,000,000 (monthly); Gaji Istri: 8,000,000 (monthly); Freelance: 2,000,000 (variable)"),
            ("fixed_obligations_total", "6850000"),
        ],
    )

    c.executemany(
        "INSERT INTO budgets (category_id, month, monthly_limit) VALUES (?,?,?)",
        [
            (2, "2026-08", 3_500_000),
            (6, "2026-08", 1_000_000),
            (8, "2026-08", 800_000),
        ],
    )

    c.executemany(
        "INSERT INTO goals (id, name, target_amount, target_date, priority) VALUES (?,?,?,?,?)",
        [
            (1, "Emergency Fund", 75_000_000, "2027-06-01", "High"),
            (2, "House Down Payment", 200_000_000, "2029-01-01", "High"),
            (3, "Anak 1 - Dana Kuliah", 150_000_000, "2032-07-01", "Medium"),
        ],
    )

    c.executemany(
        "INSERT INTO goal_contributions (id, goal_id, date, amount, source, notes) VALUES (?,?,?,?,?,?)",
        [
            (1, 1, "2026-07-01", 2_000_000, "monthly allocation", "Auto transfer"),
            (2, 1, "2026-08-01", 2_000_000, "monthly allocation", "Auto transfer"),
            (3, 2, "2026-07-15", 3_000_000, "monthly allocation", "Manual transfer"),
            (4, 2, "2026-08-15", 1_500_000, "leftover after budget", "Sisa bulan ini"),
            (5, 3, "2026-08-01", 500_000, "monthly allocation", "Reksadana pendidikan"),
        ],
    )

    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the finance SQLite DB.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"Path to the SQLite DB file (default: {DEFAULT_DB})")
    parser.add_argument("--reset", action="store_true",
                        help="Drop all tables before recreating.")
    parser.add_argument("--seed", action="store_true",
                        help="Load sample data from the schema doc.")
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(args.db)
    try:
        if args.reset:
            drop_all(conn)
            print(f"Dropped existing tables in {args.db}")
        create_schema(conn)
        conn.commit()
        print(f"Schema ready in {args.db}")

        if args.seed:
            # Guard against double-seeding when not resetting.
            existing = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if existing and not args.reset:
                print("Data already present; skipping seed (use --reset --seed to reload).")
            else:
                seed(conn)
                print("Sample data loaded.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
