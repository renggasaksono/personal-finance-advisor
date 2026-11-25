"""Transfer rules depend on what the household tracks (classify.Context)."""
import pytest

from classify import Context, classify_direction, is_transfer, load_context

# BCA CSV descriptions (multi-line cells joined with ' / ').
CARD_PAY = "KARTU KREDIT/PL / 0100 BCA CARD [CARD] BUDI SANTOSO"
GOPAY_VA = "TRSF E-BANKING DB / 2609/FTFVA/WS00001 / 70001/GOPAY TOPUP / [PHONE]"
SHOPEE_VA = "TRSF E-BANKING DB / 0110/FTFVA/WS00001 / 12208/SHOPEEPAY / 100000001"
FLAZZ = "FLAZZ BCA / TOPUP08100000000 0000111122223333 000000000 000001"
OWN_OUT = "BI-FAST DB / TRANSFER KE 213 BUDI SANTOSO M-BCA"
OWN_IN = "BI-FAST CR / TRANSFER DR 008 BUDI SANTOSO"
OTHER_IN = "BI-FAST CR / TRANSFER DR 022 PT CONTOH KLIEN"
SHOP_QR = "TRANSAKSI DEBIT / TGL: 1003 QR 014 00000.00BUDI SANTOSO CAFE"

TRACKS_ALL = Context(has_credit_account=True, ewallets=frozenset({"gopay", "shopeepay"}),
                     owner_names=("budi santoso",))
TRACKS_NONE = Context(has_credit_account=False, ewallets=frozenset(), owner_names=())


@pytest.mark.parametrize("line", [CARD_PAY, GOPAY_VA, SHOPEE_VA, OWN_OUT, OWN_IN])
def test_own_account_moves_are_transfers_when_tracked(line):
    assert is_transfer(line, "bank", TRACKS_ALL)


@pytest.mark.parametrize("line", [CARD_PAY, GOPAY_VA, SHOPEE_VA, OWN_OUT, OWN_IN])
def test_untracked_destination_is_not_a_transfer(line):
    """If the card / wallet / other account is never imported, the bank row is
    the only record of that money, so it must stay spending (or income)."""
    assert not is_transfer(line, "bank", TRACKS_NONE)


def test_wallet_matching_is_per_wallet():
    gopay_only = Context(ewallets=frozenset({"gopay"}))
    assert is_transfer(GOPAY_VA, "bank", gopay_only)
    assert not is_transfer(SHOPEE_VA, "bank", gopay_only)   # ShopeePay not tracked


@pytest.mark.parametrize("line", [
    FLAZZ,                                     # e-money card, not a tracked wallet
    "TOP UP PULSA TELKOMSEL 100.000",          # phone credit
    "NOVOTEL TOPUP DEPOSIT 500.000",           # 'ovo' inside a word is not OVO
    OTHER_IN,                                  # someone else paying in
    SHOP_QR,                                   # owner name, but a shop payment
    "BI-FAST DB / BIAYA TXN KE 213 BUDI SANTOSO M-BCA",   # the transfer's fee
])
def test_not_transfers(line):
    assert not is_transfer(line, "bank", TRACKS_ALL)


def test_card_statement_payment_always_transfer():
    # Reading the card itself, so it is tracked by definition.
    assert classify_direction("PEMBAYARAN - MYBCA 1.320.000 CR", "cc", TRACKS_NONE) == "transfer"


def test_ewallet_statement_topup_always_transfer():
    assert classify_direction("Top Up 500.000", "ewallet", TRACKS_NONE) == "transfer"


def test_load_context(seeded_conn):
    seeded_conn.execute("INSERT INTO accounts (name, type) VALUES ('BCA Card', 'credit')")
    seeded_conn.execute("INSERT INTO family_profile (key, value) VALUES "
                        "('owner_names', ' Budi  Santoso , Siti Rahma')")
    ctx = load_context(seeded_conn)
    assert ctx.has_credit_account
    assert ctx.ewallets == {"gopay", "ovo"}          # seed e-wallet accounts
    assert ctx.owner_names == ("budi santoso", "siti rahma")
    assert is_transfer("BI-FAST DB / TRANSFER KE 009 SITI RAHMA", "bank", ctx)


def test_seed_without_card_or_owner(seeded_conn):
    ctx = load_context(seeded_conn)
    assert not ctx.has_credit_account and ctx.owner_names == ()
    assert classify_direction("PEMBAYARAN KARTU KREDIT BCA 3.000.000", "bank", ctx) == "expense"
