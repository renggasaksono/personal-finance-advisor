import sqlite3

import pytest

import init_db

EXPECTED_TABLES = set(init_db._DROP_ORDER)


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_create_schema_creates_every_table(conn):
    assert EXPECTED_TABLES <= _tables(conn)


def test_create_schema_is_idempotent(conn):
    init_db.create_schema(conn)
    init_db.create_schema(conn)
    assert EXPECTED_TABLES <= _tables(conn)


def test_drop_order_covers_all_tables(conn):
    assert _tables(conn) - {"sqlite_sequence"} == EXPECTED_TABLES


def test_drop_all_removes_tables(conn):
    init_db.drop_all(conn)
    assert _tables(conn) & EXPECTED_TABLES == set()


def test_seed_loads_sample_data(seeded_conn):
    q = lambda t: seeded_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert q("accounts") == 4
    assert q("categories") == 10
    assert q("transactions") == 6
    assert q("goals") == 3
    # Parent link resolved by name.
    parent = seeded_conn.execute(
        "SELECT p.name FROM categories c JOIN categories p ON c.parent_id=p.id "
        "WHERE c.name='Groceries'").fetchone()[0]
    assert parent == "Household"


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO budgets (category_id, month, monthly_limit) "
                     "VALUES (999, '2026-08', 1)")


@pytest.mark.parametrize("col,val", [
    ("type", "bogus"),
    ("source", "telepathy"),
])
def test_transaction_check_constraints(conn, col, val):
    cols = {"date": "2026-08-01", "amount": 1, "type": "expense", "source": "manual"}
    cols[col] = val
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions (date, amount, type, source) VALUES (?,?,?,?)",
            (cols["date"], cols["amount"], cols["type"], cols["source"]))


def test_dedup_hash_unique_but_null_allowed(conn):
    ins = ("INSERT INTO transactions (date, amount, dedup_hash) "
           "VALUES ('2026-08-01', 1, ?)")
    conn.execute(ins, (None,))
    conn.execute(ins, (None,))          # many NULLs are fine (partial index)
    conn.execute(ins, ("abc",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("abc",))
