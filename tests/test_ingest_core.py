import pytest

from ingest_core import commit_transactions, dedup_hash, normalize_merchant


def row(date="2026-08-01", amount=50_000, merchant="Indomaret", acct=None,
        ext=None, ttype="expense"):
    base = (date, amount, ttype, None, merchant, acct, None, "sheet", None)
    return base + ((ext,) if ext is not None else ())


@pytest.mark.parametrize("raw,expected", [
    ("Indomaret", "indomaret"),
    ("  INDOMARET -- Pt. ", "indomaret pt"),
    ("Apotek  K-24", "apotek k 24"),
    (None, ""),
    ("", ""),
])
def test_normalize_merchant(raw, expected):
    assert normalize_merchant(raw) == expected


def test_hash_ignores_sign_and_merchant_formatting():
    a = dedup_hash("2026-08-01", -50_000, "expense", "INDOMARET.", 1, None, 0)
    b = dedup_hash("2026-08-01", 50_000, "expense", "indomaret", 1, None, 0)
    assert a == b


def test_hash_differs_by_occurrence_account_and_type():
    base = dedup_hash("2026-08-01", 1, "expense", "x", 1, None, 0)
    assert base != dedup_hash("2026-08-01", 1, "expense", "x", 1, None, 1)
    assert base != dedup_hash("2026-08-01", 1, "expense", "x", 2, None, 0)
    assert base != dedup_hash("2026-08-01", 1, "income", "x", 1, None, 0)


def test_external_ref_dominates_content():
    a = dedup_hash("2026-08-01", 1, "expense", "a", 1, "REF1", 0)
    b = dedup_hash("2027-01-01", 999, "income", "b", 1, "REF1", 5)
    assert a == b
    assert a != dedup_hash("2026-08-01", 1, "expense", "a", 2, "REF1", 0)


def test_commit_inserts_and_reimport_is_noop(conn):
    rows = [row(), row(date="2026-08-02")]
    assert commit_transactions(conn, rows) == (2, 0)
    assert commit_transactions(conn, rows) == (0, 2)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_identical_rows_in_one_batch_are_all_kept(conn):
    # Two coffees, same price, same day — real, not duplicates.
    assert commit_transactions(conn, [row(), row()]) == (2, 0)
    # Re-importing that same pair is still a no-op...
    assert commit_transactions(conn, [row(), row()]) == (0, 2)
    # ...but a statement with a third identical row adds just that one.
    assert commit_transactions(conn, [row(), row(), row()]) == (1, 2)


def test_overlapping_statement_only_adds_new_rows(conn):
    commit_transactions(conn, [row(date="2026-08-01"), row(date="2026-08-02")])
    ins, dups = commit_transactions(
        conn, [row(date="2026-08-02"), row(date="2026-08-03")])
    assert (ins, dups) == (1, 1)


def test_amount_stored_as_absolute_and_ext_ref_persisted(conn):
    commit_transactions(conn, [row(amount=-75_000, ext="TXN-9")])
    amount, ext, h = conn.execute(
        "SELECT amount, external_ref, dedup_hash FROM transactions").fetchone()
    assert amount == 75_000
    assert ext == "TXN-9"
    assert len(h) == 64


def test_empty_batch(conn):
    assert commit_transactions(conn, []) == (0, 0)
