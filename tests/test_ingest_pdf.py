import pytest

import ingest_pdf
from ingest_pdf import _find_date, _statement_anchor, parse_statement_local

BCA_CC = """\
BCA KARTU KREDIT
TANGGAL TAGIHAN 03 NOVEMBER 2025
NO. KARTU 4111 1111 1111 1111
TGL TRANSAKSI TGL PEMBUKUAN KETERANGAN JUMLAH
06-OKT 07-OKT INDOMARET JAKARTA 125.000
15-OKT 16-OKT TOKOPEDIA CICILAN KE 05 DARI 12 1.250.000
20-OKT 20-OKT PEMBAYARAN - TERIMA KASIH 3.000.000 CR
28-DES 29-DES DECEMBER CHARGE 50.000
PROMO BERLAKU HINGGA 31 DESEMBER 2025 HUBUNGI 1500888
"""


def test_statement_anchor():
    assert _statement_anchor(BCA_CC) == (2025, 11)
    assert _statement_anchor("no header here") is None


@pytest.mark.parametrize("line,anchor,expected", [
    ("2025-10-06 GRAB 45.000", None, "2025-10-06"),
    ("06/10/2025 GRAB 45.000", None, "2025-10-06"),
    ("06-OKT INDOMARET 125.000", (2025, 11), "2025-10-06"),
    ("3 Agu Shop 10.000", (2025, 11), "2025-08-03"),
    ("15-DES item 10.000", (2026, 1), "2025-12-15"),   # year rollback
    ("06-OKT INDOMARET 125.000", None, None),          # no year known
    ("31 DESEMBER 2025 promo", (2025, 11), None),      # full header date skipped
    ("CICILAN KE 05 DARI 12", (2025, 11), None),       # not a month name
])
def test_find_date(line, anchor, expected):
    assert _find_date(line, anchor)[0] == expected


def test_parse_statement_local_bca():
    rows = parse_statement_local(BCA_CC)
    simple = [(r["date"], r["amount"], r["direction"]) for r in rows]
    assert simple == [
        ("2025-10-06", 125_000, "expense"),
        ("2025-10-15", 1_250_000, "expense"),
        ("2025-10-20", 3_000_000, "transfer"),  # card bill payment, not income
        ("2024-12-28", 50_000, "expense"),     # Dec > Nov anchor -> prior year
    ]
    assert rows[0]["description"] == "INDOMARET JAKARTA"
    # Posting date stripped, but 'KE 05 DARI 12' kept in the description.
    assert "KE 05 DARI 12" in rows[1]["description"]
    assert not any(r["ambiguous"] for r in rows)


def test_parse_statement_local_flags_running_balance():
    rows = parse_statement_local("2026-08-01 GRAB 45.000 1.955.000")
    assert rows[0]["amount"] == 1_955_000      # trailing column taken
    assert rows[0]["ambiguous"] is True


@pytest.mark.parametrize("line,source_type,expected", [
    # Card bill payment — card side and bank side are both transfers.
    ("PEMBAYARAN - TERIMA KASIH 3.000.000 CR", None, "transfer"),
    ("PAYMENT THANK YOU 3.000.000 CR", "cc", "transfer"),
    ("PEMBAYARAN VIA MYBCA 3.000.000 CR", "cc", "transfer"),
    ("PEMBAYARAN KARTU KREDIT BCA 3.000.000", "bank", "transfer"),
    ("BAYAR KK MANDIRI 1.500.000", None, "transfer"),
    ("TRSF E-BANKING DB KARTU KREDIT 1.500.000", "bank", "transfer"),
    # ...but card fees and other bills are still spending.
    ("BIAYA TAHUNAN KARTU KREDIT 300.000", "bank", "expense"),
    ("BIAYA TAHUNAN KARTU 300.000", "cc", "expense"),
    ("PEMBAYARAN PLN PRABAYAR 250.000", "bank", "expense"),  # used to be 'income'
    # E-wallet top-ups move your own money; phone-credit top-ups are spending.
    ("TOP UP GOPAY 500.000", "bank", "transfer"),
    ("OVO TOPUP 200.000", None, "transfer"),
    ("Top Up 500.000", "ewallet", "transfer"),
    ("TOP UP PULSA TELKOMSEL 100.000", "bank", "expense"),
    # Real credits are still income.
    ("REFUND TOKOPEDIA 125.000", "cc", "income"),
    ("CASHBACK 10.000 CR", "ewallet", "income"),
    ("TRANSFER MASUK GAJI 15.000.000 CR", "bank", "income"),
    ("INDOMARET 125.000", "cc", "expense"),
])
def test_classify_direction(line, source_type, expected):
    assert ingest_pdf.classify_direction(line, source_type) == expected


def test_credit_regex_does_not_match_inside_words():
    rows = parse_statement_local("2026-08-01 CREDIT CARD FEE 25.000")
    assert rows[0]["direction"] == "expense"


# ---- password handling ---------------------------------------------------

@pytest.fixture
def locked_pdf(tmp_path):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.encrypt("s3cret")
    p = tmp_path / "locked.pdf"
    with open(p, "wb") as f:
        w.write(f)
    return p


def test_check_password(locked_pdf):
    assert ingest_pdf.is_encrypted(locked_pdf)
    with pytest.raises(ingest_pdf.NeedPassword):
        ingest_pdf._check_password(locked_pdf, None)
    with pytest.raises(ingest_pdf.NeedPassword):
        ingest_pdf._check_password(locked_pdf, "wrong")
    ingest_pdf._check_password(locked_pdf, "s3cret")   # no raise


# ---- end-to-end ingest (text extraction stubbed) -------------------------

@pytest.fixture
def fake_pdf(tmp_path, monkeypatch):
    """A file on disk (for hashing) whose 'extracted text' is BCA_CC."""
    p = tmp_path / "bca_nov.pdf"
    p.write_bytes(b"%PDF-1.4 fake statement bytes")
    monkeypatch.setattr(ingest_pdf, "extract_text", lambda path, pw=None: (BCA_CC, "text"))
    return p


def test_ingest_writes_rows_and_statement(seeded_db_path, seeded_conn, fake_pdf):
    n = ingest_pdf.ingest([fake_pdf], seeded_db_path, "BCA Checking", "cc",
                          False, "unused", False, False)
    assert n == 4
    rows = seeded_conn.execute(
        "SELECT date, amount, type, category_id, merchant, source FROM transactions "
        "WHERE source='statement' ORDER BY date").fetchall()
    assert len(rows) == 4
    indomaret = next(r for r in rows if r[0] == "2025-10-06")
    assert indomaret[3] == 2               # categorized via merchant map substring
    st = seeded_conn.execute(
        "SELECT file_name, source_type, period_start, period_end, row_count "
        "FROM statements").fetchone()
    assert st == ("bca_nov.pdf", "cc", "2024-12-28", "2025-10-20", 4)


def test_same_file_is_skipped_unless_forced(seeded_db_path, fake_pdf):
    args = (seeded_db_path, None, "cc", False, "unused", False)
    assert ingest_pdf.ingest([fake_pdf], *args, False) == 4
    assert ingest_pdf.ingest([fake_pdf], *args, False) == 0    # file hash seen
    assert ingest_pdf.ingest([fake_pdf], *args, True) == 0     # forced, rows dedup


def test_dry_run(seeded_db_path, seeded_conn, fake_pdf):
    assert ingest_pdf.ingest([fake_pdf], seeded_db_path, None, "cc", False,
                             "unused", True, False) == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 0


def test_card_numbers_stripped_from_stored_description(seeded_db_path, seeded_conn,
                                                       tmp_path, monkeypatch):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"x")
    text = "2026-08-01 SHOP 4111111111111111 125.000"
    monkeypatch.setattr(ingest_pdf, "extract_text", lambda path, pw=None: (text, "text"))
    ingest_pdf.ingest([p], seeded_db_path, None, None, False, "m", False, False)
    merch = seeded_conn.execute(
        "SELECT merchant FROM transactions WHERE source='statement'").fetchone()[0]
    assert "4111" not in merch


def test_ai_path_redacts_before_send(seeded_db_path, tmp_path, monkeypatch):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"x")
    text = "2026-08-01 SHOP 125.000\nemail me: a@b.co card 4111 1111 1111 1111"
    monkeypatch.setattr(ingest_pdf, "extract_text", lambda path, pw=None: (text, "text"))
    sent = {}

    def fake_ai(t, model):
        sent["text"] = t
        return [{"date": "2026-08-01", "description": "SHOP", "amount": 125_000,
                 "direction": "expense", "ambiguous": False}]
    monkeypatch.setattr(ingest_pdf, "parse_statement_ai", fake_ai)
    assert ingest_pdf.ingest([p], seeded_db_path, None, None, True, "m", False, False) == 1
    assert "a@b.co" not in sent["text"] and "4111" not in sent["text"]
    assert "125.000" in sent["text"]


def test_collect_pdfs(tmp_path):
    (tmp_path / "b.PDF").write_bytes(b"")
    (tmp_path / "a.pdf").write_bytes(b"")
    (tmp_path / "c.txt").write_text("")
    assert [p.name for p in ingest_pdf.collect_pdfs(tmp_path)] == ["a.pdf", "b.PDF"]
    with pytest.raises(SystemExit):
        ingest_pdf.collect_pdfs(tmp_path / "c.txt")


def test_ai_rows_get_transfer_backstop(seeded_db_path, seeded_conn, tmp_path, monkeypatch):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"x")
    monkeypatch.setattr(ingest_pdf, "extract_text", lambda path, pw=None: ("t", "text"))
    monkeypatch.setattr(ingest_pdf, "parse_statement_ai", lambda t, m: [
        {"date": "2026-08-01", "description": "PEMBAYARAN KARTU KREDIT",
         "amount": 3_000_000, "direction": "expense", "ambiguous": False}])
    ingest_pdf.ingest([p], seeded_db_path, None, "bank", True, "m", False, False)
    assert seeded_conn.execute("SELECT type FROM transactions WHERE source='statement'"
                               ).fetchone()[0] == "transfer"
