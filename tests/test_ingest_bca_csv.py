"""BCA internet-banking CSV export: blank DB/CR header, English-format amounts,
multi-line descriptions, running balance. Synthetic data with the real layout."""
import pytest

import ingest_sheet
from ingest_sheet import parse_amount

BCA_CSV = '''Date,Description,Branch,Amount,,Balance
01/10/2026,BIAYA ADM,0,"19,000.00",DB,"981,000.00"
01/10/2026,"KR OTOMATIS
NTRF@1000000001 @BCA00000000001 @ @PT MAJU JAYA",1,"13,500,000.00",CR,"14,481,000.00"
02/10/2026,"TRSF E-BANKING DB
0110/FTFVA/WS00001
12208/SHOPEEPAY
100000001",0,"2,000,000.00",DB,"12,481,000.00"
03/10/2026,"TRANSAKSI DEBIT
TGL: 1003 QR 014 00000.00INDOMARET KEMANG",0,"85,500.00",DB,"12,395,500.00"
05/10/2026,"KARTU KREDIT/PL
0100 BCA CARD 4111111111111111 BUDI SANTOSO",0,"1,320,000.00",DB,"11,075,500.00"
06/10/2026,"TRSF E-BANKING DB
0610/FTFVA/WS00001
70001/GOPAY TOPUP
081234567890",0,"300,000.00",DB,"10,775,500.00"
07/10/2026,"BI-FAST DB
TRANSFER KE 213 BUDI SANTOSO M-BCA",0,"1,000,000.00",DB,"9,775,500.00"
08/10/2026,"BI-FAST CR
TRANSFER DR 008 BUDI SANTOSO",0,"500,000.00",CR,"10,275,500.00"
09/10/2026,"BI-FAST CR
TRANSFER DR 022 PT CONTOH KLIEN",0,"2,500,000.00",CR,"12,775,500.00"
10/10/2026,"TARIKAN ATM
TARIKAN ATM 10/10",998,"200,000.00",DB,"12,575,500.00"
'''


@pytest.mark.parametrize("raw,expected", [
    ("1,000,000.00", 1_000_000),    # English format (BCA CSV)
    ("79,813.35", 79_813),
    ("350,000", 350_000),           # was parsed as 350
    ("5000.00", 5_000),             # was parsed as 500,000
    ("12500.00", 12_500),
    ("3.500.000,00", 3_500_000),    # Indonesian format unchanged
    ("1.250", 1_250),
    ("12,6", 13),
])
def test_parse_amount_both_conventions(raw, expected):
    assert parse_amount(raw) == expected


@pytest.fixture
def bca_db(seeded_db_path, seeded_conn):
    """Seed + a bank account; GoPay (seed) is tracked, ShopeePay is not."""
    seeded_conn.execute("INSERT INTO accounts (name, type) VALUES ('BCA Tahapan', 'bank')")
    seeded_conn.commit()
    return seeded_db_path


def _write(tmp_path):
    p = tmp_path / "BCA Tahapan Statements - October 2026.csv"
    p.write_text(BCA_CSV, encoding="utf-8")
    return p


def _rows(conn):
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT t.date, t.amount, t.type, t.merchant FROM transactions t "
        "JOIN accounts a ON a.id=t.account_id WHERE a.name='BCA Tahapan'")}


def test_direction_column_detected_by_values():
    import pandas as pd
    df = ingest_sheet.normalize_columns(
        pd.DataFrame({"Date": ["x"], "Amount": ["1"], "Unnamed: 4": ["DB"], "Branch": ["0"]}))
    assert "direction" in df.columns and "Branch" in df.columns


def test_bca_csv_types_without_card_or_owner(bca_db, seeded_conn, tmp_path):
    """No credit account and no owner_names: card payments and own-name
    transfers are the only record of that money, so they stay as spent/earned."""
    assert ingest_sheet.ingest(_write(tmp_path), bca_db, "BCA Tahapan", None, False) == 10
    rows = _rows(seeded_conn)
    assert rows["2026-10-01"][:2] in {(19_000, "expense"), (13_500_000, "income")}
    assert rows["2026-10-05"][:2] == (1_320_000, "expense")    # no card account
    assert rows["2026-10-06"][:2] == (300_000, "transfer")     # GoPay is tracked
    assert rows["2026-10-02"][:2] == (2_000_000, "expense")    # ShopeePay is not
    assert rows["2026-10-08"][:2] == (500_000, "income")       # no owner_names yet
    assert rows["2026-10-09"][:2] == (2_500_000, "income")
    assert rows["2026-10-10"][:2] == (200_000, "expense")      # ATM, no cash account


def test_bca_csv_with_card_and_owner(bca_db, seeded_conn, tmp_path):
    seeded_conn.execute("INSERT INTO accounts (name, type) VALUES ('BCA Credit Card', 'credit')")
    seeded_conn.execute("INSERT INTO family_profile (key, value) VALUES ('owner_names', 'Budi Santoso')")
    seeded_conn.commit()
    ingest_sheet.ingest(_write(tmp_path), bca_db, "BCA Tahapan", None, False)
    rows = _rows(seeded_conn)
    assert rows["2026-10-05"][1] == "transfer"     # card bill payment
    assert rows["2026-10-07"][1] == "transfer"     # to own account elsewhere
    assert rows["2026-10-08"][1] == "transfer"     # from own account elsewhere
    assert rows["2026-10-09"][1] == "income"       # someone else paying in
    income = seeded_conn.execute(
        "SELECT SUM(amount) FROM transactions WHERE type='income' AND date LIKE '2026-10%'"
    ).fetchone()[0]
    assert income == 13_500_000 + 2_500_000


def test_descriptions_are_one_line_and_redacted(bca_db, seeded_conn, tmp_path):
    ingest_sheet.ingest(_write(tmp_path), bca_db, "BCA Tahapan", None, False)
    rows = _rows(seeded_conn)
    card = rows["2026-10-05"][2]
    assert "\n" not in card and card.startswith("KARTU KREDIT/PL / 0100 BCA CARD")
    assert "4111" not in card                       # card number masked
    assert "081234567890" not in rows["2026-10-06"][2]   # phone masked


def test_categorized_by_substring(bca_db, seeded_conn, tmp_path):
    ingest_sheet.ingest(_write(tmp_path), bca_db, "BCA Tahapan", None, False)
    cat = seeded_conn.execute(
        "SELECT c.name FROM transactions t JOIN categories c ON c.id=t.category_id "
        "WHERE t.date='2026-10-03'").fetchone()[0]
    assert cat == "Groceries"                       # 'Indomaret' map entry


def test_reimport_is_noop(bca_db, tmp_path):
    p = _write(tmp_path)
    assert ingest_sheet.ingest(p, bca_db, "BCA Tahapan", None, False) == 10
    assert ingest_sheet.ingest(p, bca_db, "BCA Tahapan", None, False) == 0


def test_dry_run_reports_opening_balance_and_totals(bca_db, seeded_conn, tmp_path, capsys):
    ingest_sheet.ingest(_write(tmp_path), bca_db, "BCA Tahapan", None, True)
    out = capsys.readouterr().out
    # First row: 19,000 debit leaving 981,000 -> 1,000,000 before it.
    assert "Opening balance before 2026-10-01: 1,000,000" in out
    assert "--balance 1000000" in out
    assert "income      3 row(s)" in out and "16,500,000" in out
    assert "Transfers (own-account moves" in out
    assert seeded_conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE date LIKE '2026-10%'").fetchone()[0] == 0
