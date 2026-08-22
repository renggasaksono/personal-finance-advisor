#!/usr/bin/env python3
"""Bring an existing finance DB up to the current schema (idempotent).

Adds the statement-ingestion + dedup features to a DB created before them:
  - transactions.external_ref, transactions.dedup_hash columns
  - UNIQUE index on dedup_hash
  - 'statement' added to the transactions.source CHECK (requires a table
    rebuild — SQLite can't alter a CHECK in place)
  - the statements table
  - backfills dedup_hash for existing rows

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

        # statements table + any other new objects from the canonical schema.
        create_schema(conn)
        conn.commit()

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
