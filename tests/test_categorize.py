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
    assert [(m, n) for m, n, *_ in rows] == [("B", 2), ("A", 1)]


def test_review_reads_input(seeded_conn, monkeypatch):
    _add_uncat(seeded_conn, "Toko Baru", "Skip Me")
    answers = {"Toko Baru": "Groceries", "Skip Me": "s"}
    monkeypatch.setattr("builtins.input",
                        lambda prompt: answers[prompt.split("  (")[0]])
    categorize.cmd_review(seeded_conn)
    assert _cat_of(seeded_conn, "Toko Baru") == 2
    assert _cat_of(seeded_conn, "Skip Me") is None


# ---- bank descriptions: stable merchant keys -----------------------------
# Synthetic rows in BCA's real description shapes (multi-line cells joined
# with ' / ', card/account/phone numbers already masked by the importer).

@pytest.mark.parametrize("description,key", [
    ("TRANSAKSI DEBIT / TGL: 0902 QR 914 00000.00Warung Sate", "Warung Sate"),
    ("TRANSAKSI DEBIT / TGL: 1017 QRC 014 00000.00Kopi Kenang", "Kopi Kenang"),
    ("KARTU DEBIT / RS SEHAT SELALU", "RS SEHAT SELALU"),
    ("DB DEBIT DOMESTIK / TRN DEBIT DOM 002 MINIMART CABANG 7", "MINIMART CABANG 7"),
    ("TRSF E-BANKING DB / 0409/FTFVA/WS00001 / 15918/ASTRO / [ACCT]", "ASTRO"),
    ("TRSF E-BANKING DB / 0110/FTFVA/WS00001 / 12208/SHOPEEPAY / 100000001", "SHOPEEPAY"),
    ("TRSF E-BANKING DB / 0610/FTFVA/WS00001 / 70001/GOPAY TOPUP / [PHONE]", "GOPAY TOPUP"),
    ("TRSF E-BANKING DB / 0410/FTSCY/WS00001 / 25000.00 / ANDI WIJAYA", "ANDI WIJAYA"),
    ("TRSF E-BANKING DB / 0410/FTSCY/WS00001 / 120000.00 / catatan / SITI RAHMA", "SITI RAHMA"),
    ("BI-FAST DB / TRANSFER KE 008 BUDI SANTOSO M-BCA", "BUDI SANTOSO"),
    ("SWITCHING DB / TRF QR KE 915 IBU YATI Kota Jakart", "IBU YATI"),
    ("BI-FAST DB / BIAYA TXN KE 008 BUDI SANTOSO M-BCA", "BIAYA TXN"),
    ("SWITCHING / BIAYA TXN DI ALTO MINIMART", "BIAYA TXN"),
    ("SWITCHING DB / FEE TRF QR KE 915 IBU YATI Kota Jakart", "FEE TRF QR"),
    ("BIAYA ADM", "BIAYA ADM"),
    ("TARIKAN ATM / TARIKAN ATM 02/09", "TARIKAN ATM"),
    ("SWITCHING / WITHDRAWAL DI ALTO MINIMART", "WITHDRAWAL"),
    ("Indomaret", "Indomaret"),               # plain (sheet / receipt) merchants unchanged
    ("Apotek K24 Cilandak", "Apotek K24 Cilandak"),
    ("", ""),
])
def test_merchant_key(description, key):
    assert categorize.merchant_key(description) == key
    # A mapping saved under the key must match the description it came from.
    if key:
        assert key.lower() in description.lower()


def test_same_merchant_next_month_has_same_key():
    sep = "TRANSAKSI DEBIT / TGL: 0902 QR 914 00000.00Warung Sate"
    oct_ = "TRANSAKSI DEBIT / TGL: 1017 QR 002 00000.00Warung Sate"
    assert categorize.merchant_key(sep) == categorize.merchant_key(oct_)


def _bank_rows(conn):
    conn.execute("INSERT INTO accounts (id, name, type) VALUES (9, 'Bank', 'bank')")
    commit_transactions(conn, [
        ("2026-09-02", 40_000, "expense", None,
         "TRANSAKSI DEBIT / TGL: 0902 QR 914 00000.00Warung Sate", 9, None, "sheet", None),
        ("2026-10-17", 45_000, "expense", None,
         "TRANSAKSI DEBIT / TGL: 1017 QR 002 00000.00Warung Sate", 9, None, "sheet", None),
        ("2026-09-01", 13_500_000, "income", None,
         "KR OTOMATIS / NTRF@[ACCT] @ @PT MAJU JAYA", 9, None, "sheet", None),
        ("2026-09-15", 1_100_000, "transfer", None,
         "KARTU KREDIT/PL / 0100 BCA CARD [CARD] BUDI SANTOSO", 9, None, "sheet", None),
    ])


def test_review_groups_by_key_and_skips_income(seeded_conn, monkeypatch):
    _bank_rows(seeded_conn)
    asked = []

    def fake_input(prompt):
        asked.append(prompt.split("  (")[0])
        return "Food/Dining" if "Warung Sate" in prompt else "s"
    seeded_conn.execute("INSERT INTO categories (name, type) VALUES ('Food/Dining', 'variable')")
    monkeypatch.setattr("builtins.input", fake_input)
    categorize.cmd_review(seeded_conn)

    assert asked == ["Warung Sate"]                    # one question for two rows; no salary
    food = seeded_conn.execute("SELECT id FROM categories WHERE name='Food/Dining'").fetchone()[0]
    cats = dict(seeded_conn.execute(
        "SELECT date, category_id FROM transactions WHERE account_id=9").fetchall())
    assert cats["2026-09-02"] == cats["2026-10-17"] == food
    assert cats["2026-09-01"] is None and cats["2026-09-15"] is None   # income/transfer untouched
    # Saved under the key, so a future month's row is categorized on import.
    mapping = categorize.load_map(seeded_conn)
    assert categorize.match_category(
        "TRANSAKSI DEBIT / TGL: 1122 QR 914 00000.00Warung Sate", mapping) == food


def test_review_skips_groups_covered_by_earlier_answer(seeded_conn, monkeypatch):
    seeded_conn.execute("INSERT INTO accounts (id, name, type) VALUES (9, 'Bank', 'bank')")
    commit_transactions(seeded_conn, [
        ("2026-09-02", 9_000, "expense", None, "BI-FAST DB / BIAYA TXN KE 008 ANDI WIJAYA M-BCA",
         9, None, "sheet", None),
        ("2026-09-03", 1_000, "expense", None, "SWITCHING / BIAYA TXN DI ALTO MINIMART",
         9, None, "sheet", None),
    ])
    asked = []
    monkeypatch.setattr("builtins.input", lambda p: asked.append(p) or "Household")
    categorize.cmd_review(seeded_conn)
    assert len(asked) == 1                            # both rows share the 'BIAYA TXN' key


def test_stats_and_apply_count_only_spending(seeded_conn, capsys):
    _bank_rows(seeded_conn)
    categorize.cmd_stats(seeded_conn)
    out = capsys.readouterr().out
    assert "Spending rows      : 8" in out          # 6 seed + 2 bank expenses
    categorize.cmd_set(seeded_conn, "MAJU JAYA", "Household")
    assert seeded_conn.execute("SELECT category_id FROM transactions WHERE type='income' "
                               "AND account_id=9").fetchone()[0] is None
