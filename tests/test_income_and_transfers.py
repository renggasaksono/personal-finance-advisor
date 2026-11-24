"""Snapshot income comes from the ledger, and moving money between your own
accounts (card bill payments, e-wallet top-ups) is neither income nor spending."""
import json

import pytest

import export_analysis
import generate_snapshot as gs
import ingest_pdf
import reconcile
from ingest_core import commit_transactions

BASELINE = 25_000_000           # family_profile.monthly_income_total in the seed
SEED_EXPENSE = 6_865_000


def _t(date, amount, ttype, acct, merchant="x", source="statement"):
    return (date, amount, ttype, None, merchant, acct, None, source, None)


@pytest.fixture
def card(seeded_conn):
    seeded_conn.execute("INSERT INTO accounts (id, name, type) VALUES (5, 'BCA Card', 'credit')")
    return 5


def test_actual_income_replaces_baseline(seeded_conn):
    commit_transactions(seeded_conn, [
        _t("2026-08-25", 15_000_000, "income", 1, "GAJI"),
        _t("2026-08-26", 2_000_000, "income", 3, "FREELANCE"),
        _t("2026-07-25", 9_999_999, "income", 1, "other month"),
    ])
    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert (s["total_income"], s["income_basis"]) == (17_000_000, "actual")
    assert s["total_savings"] == 17_000_000 - SEED_EXPENSE


def test_baseline_when_no_income_recorded(seeded_conn):
    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert (s["total_income"], s["income_basis"]) == (BASELINE, "baseline")


def test_card_refunds_are_not_income(seeded_conn, card):
    commit_transactions(seeded_conn, [_t("2026-08-12", 125_000, "income", card, "REFUND")])
    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert s["income_basis"] == "baseline"          # the refund alone doesn't count


def test_income_on_debt_named_account_is_not_income(seeded_conn):
    seeded_conn.execute("INSERT INTO accounts (id, name, type) VALUES (6, 'Kredivo', 'other')")
    seeded_conn.execute("INSERT INTO debts (name, current_balance) VALUES ('Kredivo', 0)")
    commit_transactions(seeded_conn, [_t("2026-08-12", 50_000, "income", 6)])
    assert gs.build_snapshot(seeded_conn, "2026-08")["income_basis"] == "baseline"


def test_transfers_are_neither_income_nor_expense(seeded_conn, card):
    commit_transactions(seeded_conn, [
        _t("2026-08-25", 15_000_000, "income", 1, "GAJI"),
        _t("2026-08-28", 3_000_000, "transfer", 1, "PEMBAYARAN KARTU KREDIT"),
        _t("2026-08-28", 3_000_000, "transfer", card, "PEMBAYARAN - TERIMA KASIH"),
        _t("2026-08-29", 500_000, "transfer", 1, "TOP UP GOPAY"),
    ])
    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert s["total_income"] == 15_000_000
    assert s["total_expense"] == SEED_EXPENSE


BANK = """\
REKENING TAHAPAN BCA  PERIODE AGUSTUS 2026
2026-08-25 TRANSFER MASUK GAJI PT MAJU 15.000.000 CR
2026-08-28 PEMBAYARAN KARTU KREDIT BCA 3.000.000
2026-08-29 TOP UP GOPAY 500.000
2026-08-30 PEMBAYARAN PLN PRABAYAR 250.000
"""
CARD = """\
TANGGAL TAGIHAN 03 SEPTEMBER 2026
10-AGU 11-AGU TOKOPEDIA 1.200.000
15-AGU 16-AGU INDOMARET 1.800.000
28-AGU 28-AGU PEMBAYARAN - TERIMA KASIH 3.000.000 CR
"""


def test_bank_plus_card_statements_count_spending_once(seeded_db_path, seeded_conn,
                                                       card, tmp_path, monkeypatch):
    """The reported bug: importing both statements used to count the 3M card
    bill as spending on top of the 3M of card charges it pays for."""
    seeded_conn.commit()
    texts = {"bank.pdf": BANK, "card.pdf": CARD}
    for name in texts:
        (tmp_path / name).write_bytes(name.encode())
    monkeypatch.setattr(ingest_pdf, "extract_text",
                        lambda path, pw=None: (texts[path.name], "text"))
    ingest_pdf.ingest([tmp_path / "bank.pdf"], seeded_db_path, "BCA Checking", "bank",
                      False, "m", False, False)
    ingest_pdf.ingest([tmp_path / "card.pdf"], seeded_db_path, "BCA Card", "cc",
                      False, "m", False, False)

    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert s["total_income"] == 15_000_000 and s["income_basis"] == "actual"
    # seed + card charges + the PLN bill; NOT the 3M payment or the 500k top-up
    assert s["total_expense"] == SEED_EXPENSE + 1_200_000 + 1_800_000 + 250_000


def test_mark_transfer_fixes_previously_imported_payment(seeded_conn, card):
    commit_transactions(seeded_conn, [
        _t("2026-08-28", 3_000_000, "expense", 1, "PEMBAYARAN KARTU KREDIT"),
        _t("2026-08-28", 3_000_000, "income", card, "PEMBAYARAN - TERIMA KASIH"),
    ])
    before = gs.build_snapshot(seeded_conn, "2026-08")["total_expense"]
    ids = [r[0] for r in seeded_conn.execute(
        "SELECT id FROM transactions WHERE merchant LIKE 'PEMBAYARAN%'")]
    reconcile.cmd_mark_transfer(seeded_conn, ids)
    assert gs.build_snapshot(seeded_conn, "2026-08")["total_expense"] == before - 3_000_000
    with pytest.raises(SystemExit):
        reconcile.cmd_mark_transfer(seeded_conn, [99999])


def test_income_basis_stored_and_exported(seeded_conn):
    gs.upsert_snapshot(seeded_conn, gs.build_snapshot(seeded_conn, "2026-08"))
    assert seeded_conn.execute("SELECT income_basis FROM snapshots").fetchone()[0] == "baseline"
    snaps = export_analysis.fetch_snapshots(seeded_conn, None, False)
    assert snaps[0]["income_basis"] == "baseline"
    assert "income_basis" in export_analysis.build_prompt(snaps)
