import json

import pytest

import generate_snapshot as gs
from ingest_core import commit_transactions

# Seed data, 2026-08: six expenses on four accounts, no income rows.
SEED_EXPENSE = 3_500_000 + 850_000 + 45_000 + 2_000_000 + 120_000 + 350_000
SEED_START = 15_000_000 + 500_000 + 750_000 + 300_000
INCOME = 25_000_000


def test_build_snapshot_on_seed(seeded_conn):
    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert s["total_income"] == INCOME
    assert s["total_expense"] == SEED_EXPENSE == 6_865_000
    assert s["total_savings"] == INCOME - SEED_EXPENSE
    assert s["savings_rate"] == round((INCOME - SEED_EXPENSE) / INCOME, 4)
    assert s["net_worth"] == SEED_START - SEED_EXPENSE
    assert s["dti_ratio"] == 0
    assert s["category_totals"]["Cicilan (Loan/Mortgage)"] == 3_500_000
    assert list(s["category_totals"])[0] == "Cicilan (Loan/Mortgage)"  # sorted desc
    assert s["budget_status"]["Groceries"] == {"actual": 850_000, "limit": 3_500_000,
                                               "over": False}


def test_budget_over_flag(seeded_conn):
    s = gs.build_snapshot(seeded_conn, "2026-08")
    seeded_conn.execute("UPDATE budgets SET monthly_limit=100000 "
                        "WHERE category_id=8")   # Entertainment, actual 350k
    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert s["budget_status"]["Entertainment"]["over"] is True


def test_goal_progress_is_cumulative_to_month(seeded_conn):
    jul = gs.goal_progress(seeded_conn, "2026-07")
    aug = gs.goal_progress(seeded_conn, "2026-08")
    assert jul["Emergency Fund"]["current"] == 2_000_000
    assert aug["Emergency Fund"]["current"] == 4_000_000
    assert aug["Emergency Fund"]["pct"] == round(4_000_000 / 75_000_000, 4)
    assert jul["Anak 1 - Dana Kuliah"]["current"] == 0


def test_uncategorized_and_non_expense_rows(seeded_conn):
    commit_transactions(seeded_conn, [
        ("2026-08-20", 40_000, "expense", None, "?", 1, None, "sheet", None),
        ("2026-08-21", 1_000_000, "income", None, "Bonus", 1, None, "sheet", None),
        ("2026-08-22", 500_000, "transfer", None, "Move", 1, None, "sheet", None),
    ])
    s = gs.build_snapshot(seeded_conn, "2026-08")
    assert s["category_totals"]["(uncategorized)"] == 40_000
    assert s["total_expense"] == SEED_EXPENSE + 40_000      # income/transfer excluded
    # Income row raises net worth; transfer is neutral.
    assert s["net_worth"] == SEED_START - SEED_EXPENSE - 40_000 + 1_000_000


def test_net_worth_includes_investments_assets_debts(seeded_conn):
    c = seeded_conn
    c.execute("INSERT INTO investments (id, name, type) VALUES (1, 'RDPU', 'reksadana')")
    c.executemany("INSERT INTO investment_valuations (investment_id, date, market_value) "
                  "VALUES (1, ?, ?)", [("2026-07-31", 5_000_000),
                                       ("2026-08-31", 6_000_000),
                                       ("2026-09-30", 9_000_000)])  # future: ignored
    c.execute("INSERT INTO assets (name, type, current_est_value) VALUES ('Car', 'vehicle', 100000000)")
    c.execute("INSERT INTO debts (name, current_balance, monthly_payment) "
              "VALUES ('KPR', 300000000, 5000000)")
    s = gs.build_snapshot(c, "2026-08")
    assert s["net_worth"] == (SEED_START - SEED_EXPENSE) + 6_000_000 + 100_000_000 - 300_000_000
    assert s["dti_ratio"] == round(5_000_000 / INCOME, 4)


def test_credit_card_account_not_double_counted(seeded_conn):
    """A card that is both an account and a debt counts once (as the debt)."""
    c = seeded_conn
    c.execute("INSERT INTO accounts (id, name, type) VALUES (5, 'BCA Card', 'other')")
    commit_transactions(c, [("2026-08-15", 2_000_000, "expense", None, "Shop", 5,
                             None, "statement", None)])
    c.execute("INSERT INTO debts (name, current_balance) VALUES ('bca card ', 2000000)")
    s = gs.build_snapshot(c, "2026-08")
    # Without the exclusion this would be -4,000,000 relative to baseline.
    assert s["net_worth"] == (SEED_START - SEED_EXPENSE) - 2_000_000


def test_no_income_profile(conn):
    s = gs.build_snapshot(conn, "2026-08")
    assert s["total_income"] == 0
    assert s["savings_rate"] is None
    assert s["dti_ratio"] is None


def test_upsert_snapshot_is_idempotent(seeded_conn):
    s = gs.build_snapshot(seeded_conn, "2026-08")
    gs.upsert_snapshot(seeded_conn, s)
    s["total_expense"] = 1
    gs.upsert_snapshot(seeded_conn, s)
    rows = seeded_conn.execute(
        "SELECT total_expense, category_totals FROM snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert json.loads(rows[0][1])["Groceries"] == 850_000


def test_months_with_data(seeded_conn):
    commit_transactions(seeded_conn, [("2026-09-01", 1, "expense", None, "x", 1,
                                       None, "sheet", None)])
    assert gs.months_with_data(seeded_conn) == ["2026-08", "2026-09"]


def test_snapshot_has_no_merchant_or_member_detail(seeded_conn):
    blob = json.dumps(gs.build_snapshot(seeded_conn, "2026-08"))
    for leaked in ("Indomaret", "Cinema XXI", "Istri", "Anak 2", "Bank Mandiri"):
        assert leaked not in blob


def _card_as_debt(c):
    c.execute("INSERT INTO accounts (id, name, type) VALUES (5, 'BCA Card', 'credit')")
    c.execute("INSERT INTO debts (name, current_balance) VALUES ('BCA Card', 2000000)")


def test_card_payment_to_debt_card_leaves_paying_account(seeded_conn):
    """The card is represented by its debts row, so a bill payment from the
    bank has no other side in the ledger: the bank balance must drop."""
    c = seeded_conn
    _card_as_debt(c)
    commit_transactions(c, [
        ("2026-08-20", 1_100_000, "transfer", None,
         "KARTU KREDIT/PL / 0100 BCA CARD [CARD] BUDI SANTOSO", 1, None, "sheet", None),
        ("2026-08-21", 1_200_000, "transfer", None,
         "PEMBAYARAN KARTU KREDIT BCA", 1, None, "sheet", None),
        # The same payment seen on the card's own statement: card is excluded.
        ("2026-08-22", 1_100_000, "transfer", None, "PEMBAYARAN - MYBCA", 5, None,
         "statement", None),
    ])
    s = gs.build_snapshot(c, "2026-08")
    assert s["net_worth"] == (SEED_START - SEED_EXPENSE) - 2_300_000 - 2_000_000
    assert s["total_expense"] == SEED_EXPENSE        # still not spending


def test_other_transfers_still_net_to_zero(seeded_conn):
    c = seeded_conn
    _card_as_debt(c)
    commit_transactions(c, [
        ("2026-08-20", 500_000, "transfer", None, "TOPUP GOPAY", 1, None, "sheet", None),
        ("2026-08-21", 9_000, "transfer", None, "BIAYA KARTU KREDIT", 1, None, "sheet", None),
    ])
    assert gs.build_snapshot(c, "2026-08")["net_worth"] == \
        (SEED_START - SEED_EXPENSE) - 2_000_000


def test_card_payment_without_debt_row_nets_to_zero(seeded_conn):
    """A credit account with no debts row is in the ledger itself, so its
    payment is a real two-sided transfer and changes nothing."""
    c = seeded_conn
    c.execute("INSERT INTO accounts (id, name, type) VALUES (5, 'BCA Card', 'credit')")
    commit_transactions(c, [("2026-08-20", 1_100_000, "transfer", None,
                             "PEMBAYARAN KARTU KREDIT BCA", 1, None, "sheet", None)])
    assert gs.build_snapshot(c, "2026-08")["net_worth"] == SEED_START - SEED_EXPENSE
