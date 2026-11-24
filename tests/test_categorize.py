import pytest

import categorize
from ingest_core import commit_transactions


@pytest.mark.parametrize("merchant,expected", [
    ("Indomaret", 2),               # exact
    ("INDOMARET", 2),               # case-insensitive
    ("Indomaret Point Kemang", 2),  # map name inside merchant
    ("K24", 7),                     # merchant inside map name
    ("Unknown Shop", None),
    ("", None),
])
def test_match_category(merchant, expected):
    mapping = [("Indomaret", 2), ("Apotek K24", 7)]
    assert categorize.match_category(merchant, mapping) == expected


def test_match_category_prefers_longest_name():
    mapping = [("Apotek", 1), ("Apotek K24", 7)]
    assert categorize.match_category("APOTEK K24 CILANDAK", mapping) == 7


def test_resolve_category_by_id_and_name(seeded_conn):
    assert categorize.resolve_category(seeded_conn, "6") == (6, "Transport")
    assert categorize.resolve_category(seeded_conn, "transport") == (6, "Transport")
    with pytest.raises(SystemExit):
        categorize.resolve_category(seeded_conn, "Nope")


def _add_uncat(conn, *merchants):
    rows = [(f"2026-08-{i + 11:02d}", 10_000 + i, "expense", None, m, 1, None,
             "sheet", None) for i, m in enumerate(merchants)]
    commit_transactions(conn, rows)


def _cat_of(conn, merchant):
    return conn.execute("SELECT category_id FROM transactions WHERE merchant=?",
                        (merchant,)).fetchone()[0]


def test_set_upserts_mapping_and_backfills(seeded_conn):
    _add_uncat(seeded_conn, "Warung Tegal", "WARUNG TEGAL Bahari", "Other")
    categorize.cmd_set(seeded_conn, "Warung Tegal", "Household")

    assert _cat_of(seeded_conn, "Warung Tegal") == 1
    assert _cat_of(seeded_conn, "WARUNG TEGAL Bahari") == 1   # substring
    assert _cat_of(seeded_conn, "Other") is None
    cid, conf = seeded_conn.execute(
        "SELECT category_id, confidence FROM merchant_category_map "
        "WHERE merchant_name='warung tegal'").fetchone()        # COLLATE NOCASE
    assert (cid, conf) == (1, "confirmed")

    # Re-mapping updates in place rather than duplicating.
    categorize.cmd_set(seeded_conn, "Warung Tegal", "Groceries")
    n = seeded_conn.execute("SELECT COUNT(*) FROM merchant_category_map "
                            "WHERE merchant_name='Warung Tegal'").fetchone()[0]
    assert n == 1


def test_backfill_never_overwrites_existing_category(seeded_conn):
    # Seed row 'Grab' is already Transport (6).
    categorize.cmd_set(seeded_conn, "Grab", "Entertainment")
    assert _cat_of(seeded_conn, "Grab") == 6


def test_apply_uses_current_map(seeded_conn):
    _add_uncat(seeded_conn, "PLN Prabayar", "Mystery")
    categorize.cmd_apply(seeded_conn)
    assert _cat_of(seeded_conn, "PLN Prabayar") == 3
    assert _cat_of(seeded_conn, "Mystery") is None


def test_uncategorized_merchants_groups_and_sorts(seeded_conn):
    _add_uncat(seeded_conn, "A", "B", "B")
    rows = categorize.uncategorized_merchants(seeded_conn)
    assert [(m, n) for m, n, _ in rows] == [("B", 2), ("A", 1)]


def test_review_reads_input(seeded_conn, monkeypatch):
    _add_uncat(seeded_conn, "Toko Baru", "Skip Me")
    answers = {"Toko Baru": "Groceries", "Skip Me": "s"}
    monkeypatch.setattr("builtins.input",
                        lambda prompt: answers[prompt.split("  (")[0]])
    categorize.cmd_review(seeded_conn)
    assert _cat_of(seeded_conn, "Toko Baru") == 2
    assert _cat_of(seeded_conn, "Skip Me") is None
