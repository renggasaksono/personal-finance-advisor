import sqlite3
import sys

import pytest

import init_db
import migrate

# transactions as it looked before dedup/statements were added.
OLD_TRANSACTIONS = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL, starting_balance INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'IDR');
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL, parent_id INTEGER);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, amount INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT 'expense', category_id INTEGER, merchant TEXT,
    account_id INTEGER, family_member TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'sheet', 'receipt')),
    notes TEXT);
INSERT INTO transactions (date, amount, merchant, source) VALUES
    ('2026-08-01', 10000, 'Kopi', 'sheet'),
    ('2026-08-01', 10000, 'Kopi', 'sheet'),     -- genuine repeat
    ('2026-08-02', 20000, 'Grab', 'manual');
"""


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(OLD_TRANSACTIONS)
    c.close()
    return path


def _run(path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["migrate.py", "--db", str(path)])
    migrate.main()


def test_migrates_old_db(old_db, monkeypatch):
    _run(old_db, monkeypatch)
    c = init_db.connect(old_db)
    cols = migrate._columns(c, "transactions")
    assert {"external_ref", "dedup_hash"} <= cols
    assert "'statement'" in migrate._table_sql(c, "transactions")
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    hashes = [r[0] for r in c.execute("SELECT dedup_hash FROM transactions")]
    assert all(hashes) and len(set(hashes)) == 3      # repeats get distinct hashes
    # New source value now accepted; statements table exists.
    c.execute("INSERT INTO transactions (date, amount, source) "
              "VALUES ('2026-08-03', 1, 'statement')")
    c.execute("SELECT COUNT(*) FROM statements")
    c.close()


def test_migrate_is_idempotent(old_db, monkeypatch):
    _run(old_db, monkeypatch)
    c = init_db.connect(old_db)
    before = c.execute("SELECT id, dedup_hash FROM transactions ORDER BY id").fetchall()
    c.close()
    _run(old_db, monkeypatch)
    c = init_db.connect(old_db)
    assert c.execute("SELECT id, dedup_hash FROM transactions ORDER BY id").fetchall() == before
    c.close()


def test_backfill_hashes_match_ingest_core(old_db, monkeypatch):
    """After migration, re-ingesting the same rows must be recognised as dups."""
    from ingest_core import commit_transactions
    _run(old_db, monkeypatch)
    c = init_db.connect(old_db)
    ins, dups = commit_transactions(c, [
        ("2026-08-01", 10000, "expense", None, "Kopi", None, None, "sheet", None),
        ("2026-08-01", 10000, "expense", None, "Kopi", None, None, "sheet", None),
    ])
    assert (ins, dups) == (0, 2)
    c.close()


def test_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["migrate.py", "--db", str(tmp_path / "nope.db")])
    with pytest.raises(SystemExit):
        migrate.main()
