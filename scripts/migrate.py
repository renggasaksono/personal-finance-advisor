#!/usr/bin/env python3
"""Bring an existing finance DB up to the current schema (idempotent).

Adds the statement-ingestion + dedup features to a DB created before them:
  - transactions.external_ref, transactions.dedup_hash columns
  - UNIQUE index on dedup_hash
  - 'statement' added to the transactions.source CHECK (requires a table
    rebuild — SQLite can't alter a CHECK in place)
  - the statements table
  - backfills dedup_hash for existing rows
And the liability / income-basis changes:
  - 'credit' added to the accounts.type CHECK (table rebuild)
  - snapshots.income_basis column
  - `--credit NAME` marks an account as a liability (credit card / paylater);
    accounts with ingested 'cc' statements are listed as candidates

Safe to run repeatedly. Usage:
    python scripts/migrate.py
    python scripts/migrate.py --db data/finance.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect, create_schema  # noqa: E402
from ingest_core import dedup_hash  # noqa: E402


def _columns(conn, table) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_sql(conn, table) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else ""


def rebuild_transactions(conn: sqlite3.Connection) -> None:
    """Rebuild transactions with the new columns + widened source CHECK."""
    print("  rebuilding transactions (new columns + 'statement' source)...")
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE transactions_new (
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
                                  CHECK (source IN ('manual','sheet','receipt','statement')),
            notes         TEXT,
            external_ref  TEXT,
            dedup_hash    TEXT
        );
        INSERT INTO transactions_new
            (id, date, amount, type, category_id, merchant, account_id,
             family_member, source, notes)
        SELECT id, date, amount, type, category_id, merchant, account_id,
               family_member, source, notes
        FROM transactions;
        DROP TABLE transactions;
        ALTER TABLE transactions_new RENAME TO transactions;
        CREATE INDEX IF NOT EXISTS idx_transactions_date     ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_dedup
            ON transactions(dedup_hash) WHERE dedup_hash IS NOT NULL;
        COMMIT;
        PRAGMA foreign_keys = ON;
    """)


def rebuild_accounts(conn: sqlite3.Connection) -> None:
    """Rebuild accounts so its type CHECK accepts 'credit'. Child tables
    reference accounts by name, so they keep pointing at the new table."""
    print("  rebuilding accounts (adds 'credit' type)...")
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE accounts_new (
            id               INTEGER PRIMARY KEY,
            name             TEXT    NOT NULL UNIQUE,
            type             TEXT    NOT NULL
                                     CHECK (type IN ('bank', 'cash', 'ewallet', 'credit', 'other')),
            starting_balance INTEGER NOT NULL DEFAULT 0,
            currency         TEXT    NOT NULL DEFAULT 'IDR'
        );
        INSERT INTO accounts_new (id, name, type, starting_balance, currency)
        SELECT id, name, type, starting_balance, currency FROM accounts;
        DROP TABLE accounts;
        ALTER TABLE accounts_new RENAME TO accounts;
        COMMIT;
        PRAGMA foreign_keys = ON;
    """)


def card_account_candidates(conn: sqlite3.Connection) -> list[str]:
    """Non-credit accounts that have received credit-card statements. Only
    reported, never converted: a checking account may have been used by mistake."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT a.name FROM accounts a "
        "JOIN statements s ON s.account_id = a.id "
        "WHERE s.source_type = 'cc' AND a.type <> 'credit' ORDER BY a.name")]


def mark_credit(conn: sqlite3.Connection, names: list[str]) -> None:
    for name in names:
        cur = conn.execute("UPDATE accounts SET type='credit' "
                           "WHERE name=? COLLATE NOCASE", (name,))
        if not cur.rowcount:
            known = [r[0] for r in conn.execute("SELECT name FROM accounts ORDER BY name")]
            raise SystemExit(f"Unknown account: {name!r}. Known: {known}")
        print(f"  marked {name!r} as a credit (liability) account.")
    conn.commit()


def backfill_dedup(conn: sqlite3.Connection) -> int:
    """Compute dedup_hash for rows that don't have one (occurrence-indexed)."""
    rows = conn.execute(
        "SELECT id, date, amount, type, merchant, account_id, external_ref "
        "FROM transactions WHERE dedup_hash IS NULL ORDER BY id"
    ).fetchall()
    seen: dict[tuple, int] = {}
    updated = 0
    for tid, date, amount, ttype, merch, acct, ext in rows:
        if ext:
            h = dedup_hash(date, amount, ttype, merch, acct, ext, 0)
        else:
            from ingest_core import normalize_merchant
            key = (date, abs(int(amount)), ttype, normalize_merchant(merch), acct)
            occ = seen.get(key, 0)
            seen[key] = occ + 1
            h = dedup_hash(date, amount, ttype, merch, acct, None, occ)
        try:
            conn.execute("UPDATE transactions SET dedup_hash=? WHERE id=?", (h, tid))
            updated += 1
        except sqlite3.IntegrityError:
            # A pre-existing duplicate row; leave hash NULL so it stays visible.
            pass
    conn.commit()
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate the finance DB schema.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--credit", action="append", metavar="NAME", default=[],
                        help="Mark an account as a credit card / liability (repeatable)")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}. Run init_db.py first.")

    conn = connect(args.db)
    try:
        print(f"Migrating {args.db} ...")
        cols = _columns(conn, "transactions")
        has_statement = "'statement'" in _table_sql(conn, "transactions")

        if "external_ref" not in cols or "dedup_hash" not in cols or not has_statement:
            rebuild_transactions(conn)
        else:
            print("  transactions already current.")

        if "'credit'" not in _table_sql(conn, "accounts"):
            rebuild_accounts(conn)

        # statements table + any other new objects from the canonical schema.
        create_schema(conn)
        if "income_basis" not in _columns(conn, "snapshots"):
            conn.execute("ALTER TABLE snapshots ADD COLUMN income_basis TEXT")
            print("  added snapshots.income_basis.")
        conn.commit()

        if args.credit:
            mark_credit(conn, args.credit)
        candidates = card_account_candidates(conn)
        if candidates:
            print(f"  these accounts have credit-card statements but aren't type "
                  f"'credit': {', '.join(candidates)}\n"
                  f"  if they are the cards themselves, run: "
                  f"python scripts/migrate.py --credit \"NAME\"")

        n = backfill_dedup(conn)
        print(f"  backfilled dedup_hash for {n} row(s).")

        # Integrity sanity check.
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        print("  foreign_key_check:", "OK" if not bad else bad)
        print("Migration complete.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
