import pandas as pd
import pytest

import ingest_sheet
from ingest_sheet import parse_amount, parse_date


@pytest.mark.parametrize("raw,expected", [
    ("Rp 3.500.000", 3_500_000),
    ("3.500.000,00", 3_500_000),
    ("1.250", 1_250),
    ("12,6", 13),                # comma = decimal; rounded to whole rupiah
    ("-45.000", -45_000),
    ("(45.000)", -45_000),
    (50000, 50_000),
    (49999.6, 50_000),
    ("", None),
    ("abc", None),
    (None, None),
    (float("nan"), None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-08-05", "2026-08-05"),
    ("2026/8/5", "2026-08-05"),
    ("05/08/2026", "2026-08-05"),   # day-first (Indonesian)
    ("05-08-2026", "2026-08-05"),
    ("05.08.2026", "2026-08-05"),
    ("  2026-08-05 ", "2026-08-05"),
    ("", None),
    ("not a date", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_normalize_columns_maps_indonesian_aliases():
    df = pd.DataFrame(columns=["Tanggal", "Jumlah", "Keterangan", "Kategori", "Catatan", "Extra"])
    cols = list(ingest_sheet.normalize_columns(df).columns)
    assert cols == ["date", "amount", "merchant", "category", "notes", "Extra"]


def _write_csv(tmp_path, text, name="aug.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


CSV = """tanggal,jumlah,keterangan,kategori,akun,tipe
05/08/2026,"Rp 125.000",Indomaret,,,
06/08/2026,"45.000",Grab,Entertainment,OVO,
07/08/2026,"10.000.000",Gaji,,,income
08/08/2026,"99.000",Warung Baru,,Unknown Bank,weird
,"1.000",Empty date,,,
bad-date,"1.000",Bad,,,
"""


def _rows(conn):
    return conn.execute(
        "SELECT t.date, t.amount, t.type, c.name, t.merchant, a.name, t.source "
        "FROM transactions t LEFT JOIN categories c ON t.category_id=c.id "
        "LEFT JOIN accounts a ON t.account_id=a.id "
        "WHERE t.id > 6 ORDER BY t.date").fetchall()   # skip the 6 seed rows


def test_ingest_csv_end_to_end(seeded_db_path, seeded_conn, tmp_path):
    before = seeded_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    path = _write_csv(tmp_path, CSV)
    inserted = ingest_sheet.ingest(path, seeded_db_path, "BCA Checking", None, False)
    assert inserted == 4

    assert _rows(seeded_conn) == [
        # merchant map -> Groceries; default account
        ("2026-08-05", 125_000, "expense", "Groceries", "Indomaret", "BCA Checking", "sheet"),
        # explicit category beats merchant map; per-row account beats default
        ("2026-08-06", 45_000, "expense", "Entertainment", "Grab", "OVO", "sheet"),
        ("2026-08-07", 10_000_000, "income", None, "Gaji", "BCA Checking", "sheet"),
        # unknown account falls back to default; invalid type -> expense
        ("2026-08-08", 99_000, "expense", None, "Warung Baru", "BCA Checking", "sheet"),
    ]
    after = seeded_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert after == before + 4


def test_reingest_same_sheet_skips_duplicates(seeded_db_path, tmp_path):
    path = _write_csv(tmp_path, CSV)
    assert ingest_sheet.ingest(path, seeded_db_path, None, None, False) == 4
    assert ingest_sheet.ingest(path, seeded_db_path, None, None, False) == 0


def test_dry_run_writes_nothing(seeded_db_path, seeded_conn, tmp_path):
    path = _write_csv(tmp_path, CSV)
    assert ingest_sheet.ingest(path, seeded_db_path, None, None, True) == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 6


def test_missing_required_column(seeded_db_path, tmp_path):
    path = _write_csv(tmp_path, "merchant,amount\nX,1000\n")
    with pytest.raises(SystemExit, match="date"):
        ingest_sheet.ingest(path, seeded_db_path, None, None, False)


def test_unknown_default_account(seeded_db_path, tmp_path):
    path = _write_csv(tmp_path, "date,amount\n2026-08-01,1000\n")
    with pytest.raises(SystemExit, match="Unknown account"):
        ingest_sheet.ingest(path, seeded_db_path, "Nope Bank", None, False)


def test_xlsx_supported(seeded_db_path, tmp_path):
    path = tmp_path / "aug.xlsx"
    pd.DataFrame({"Date": ["2026-08-20"], "Amount": ["Rp 75.000"],
                  "Merchant": ["Apotek K24"]}).to_excel(path, index=False)
    assert ingest_sheet.ingest(path, seeded_db_path, None, None, False) == 1


def test_unsupported_extension(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hi")
    with pytest.raises(ValueError):
        ingest_sheet.read_sheet(p, None)
