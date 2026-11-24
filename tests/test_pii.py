import pytest

from pii import _luhn_ok, redact, redact_field, redact_names

VISA_TEST = "4111111111111111"      # Luhn-valid test card


def test_luhn():
    assert _luhn_ok(VISA_TEST)
    assert not _luhn_ok("4111111111111112")


@pytest.mark.parametrize("text,label", [
    ("contact budi.s+bank@mail.co.id now", "EMAIL"),
    ("NPWP 01.234.567.8-901.000", "NPWP"),
    ("call 0812-3456-7890", "PHONE"),
    ("call +6281234567890", "PHONE"),
    ("card 4111 1111 1111 1111", "CARD"),
    ("card 4111-1111-1111-1111", "CARD"),
    (f"card {VISA_TEST}", "CARD"),
    ("rek 1234567890", "ACCT"),
])
def test_redacts_identifiers(text, label):
    out, counts = redact(text)
    assert f"[{label}]" in out
    assert counts[label] == 1


@pytest.mark.parametrize("amount", [
    "Rp 3.500.000", "3.500.000,00", "1,250,000", "Rp1250000", "12.500",
])
def test_amounts_survive(amount):
    out, counts = redact(f"06-OKT INDOMARET {amount}")
    assert amount in out
    assert not counts


@pytest.mark.parametrize("line,expected", [
    # Regression: the card pattern used to swallow the amount's first digit.
    ("06-OKT TOKOPEDIA 4111 1111 1111 1111 1.250.000",
     "06-OKT TOKOPEDIA [CARD] 1.250.000"),
    ("06-OKT TOKOPEDIA 4111-1111-1111-1111 12.500,00",
     "06-OKT TOKOPEDIA [CARD] 12.500,00"),
    ("06-OKT TOKOPEDIA 4111 1111 1111 1111 125000",
     "06-OKT TOKOPEDIA [CARD] 125000"),
])
def test_statement_line_keeps_amount_drops_card(line, expected):
    out, _ = redact(line)
    assert out == expected


def test_ai_parse_after_redaction_keeps_amount():
    """End-to-end form of the bug: redact, then parse, amount must survive."""
    from ingest_pdf import parse_statement_local
    text, _ = redact("2026-08-01 TOKOPEDIA 4111 1111 1111 1111 1.250.000")
    assert parse_statement_local(text)[0]["amount"] == 1_250_000


def test_redact_field_handles_empty():
    assert redact_field(None) is None
    assert redact_field("") == ""
    assert redact_field("GRAB 1234567890") == "GRAB [ACCT]"


def test_redact_names_masks_address_lines():
    text = "BUDI SANTOSO\nJl. Sudirman No. 1\nRT 2 RW 3\nTOTAL 100.000"
    out, n = redact_names(text)
    assert n == 2
    assert out.splitlines() == ["BUDI SANTOSO", "[ADDRESS]", "[ADDRESS]", "TOTAL 100.000"]


@pytest.mark.parametrize("line", [
    "RT 002 / RW 03",          # regression: multi-digit RT/RW used to slip through
    "RT.05 RW.011",
    "Kel. Menteng",            # regression: '\\b' after '.' never matched
    "Kec. Setiabudi",
    "Jl. Sudirman Kav. 1",
    "JALAN MH THAMRIN",
    "Kode Pos 12190",
])
def test_redact_names_address_forms(line):
    out, n = redact_names(line)
    assert (out, n) == ("[ADDRESS]", 1)


@pytest.mark.parametrize("line", [
    "06-OKT INDOMARET 125.000",
    "HOTEL. MULIA 2.500.000",  # 'kel.' only at a word start
    "SMART 2 TV 1.000.000",    # 'rt' inside a word
    "JLX STORE 10.000",
])
def test_redact_names_leaves_transactions_alone(line):
    assert redact_names(line) == (line, 0)
