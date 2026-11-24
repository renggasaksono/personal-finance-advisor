import pytest

import generate_snapshot as gs
import setup
from setup import SetupError, months_between, parse_idr


@pytest.mark.parametrize("raw,expected", [
    ("15000000", 15_000_000),
    ("Rp 15.000.000", 15_000_000),
    ("15jt", 15_000_000),
    ("15 juta", 15_000_000),
    ("1,5jt", 1_500_000),
    ("1.5jt", 1_500_000),        # decimal point is fine with a suffix
    ("750rb", 750_000),
    ("750 ribu", 750_000),
    ("50k", 50_000),
    ("Rp 2,5JT", 2_500_000),
])
def test_parse_idr(raw, expected):
    assert parse_idr(raw) == expected


@pytest.mark.parametrize("raw", ["lots", "5jtt", "2 juta lebih", "15m", "Rp", ""])
def test_parse_idr_rejects_unclear_input(raw):
    with pytest.raises(SetupError):
        parse_idr(raw)


def test_parse_idr_negatives():
    with pytest.raises(SetupError):
        parse_idr("-5jt")
    assert parse_idr("-5jt", allow_negative=True) == -5_000_000
    assert parse_idr("-Rp 45.000", allow_negative=True) == -45_000
    assert parse_idr("(45.000)", allow_negative=True) == -45_000


def test_months_between():
    assert months_between("2026-11", "2027-02") == ["2026-11", "2026-12", "2027-01", "2027-02"]
    assert months_between("2026-11", "2026-11") == ["2026-11"]
    with pytest.raises(SetupError):
        months_between("2026-11", "2026-10")


@pytest.fixture
def run(db_path):
    def _run(*argv):
        setup.main(["--db", str(db_path), *argv])
    return _run


def _q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


# ---- accounts ------------------------------------------------------------

def test_account_lifecycle(run, conn, capsys):
    run("account", "add", "BCA Checking", "--type", "bank", "--balance", "12,5jt")
    run("account", "add", "BCA Credit Card", "--type", "credit")
    run("account", "add", "Overdrawn", "--type", "bank", "--balance", "-200rb")
    assert _q(conn, "SELECT name, type, starting_balance FROM accounts ORDER BY id") == [
        ("BCA Checking", "bank", 12_500_000),
        ("BCA Credit Card", "credit", 0),
        ("Overdrawn", "bank", -200_000),
    ]
    run("account", "set", "bca checking", "--balance", "13jt", "--rename", "BCA Tahapan")
    assert _q(conn, "SELECT name, starting_balance FROM accounts WHERE id=1") == [
        ("BCA Tahapan", 13_000_000)]
    run("account", "remove", "Overdrawn")
    capsys.readouterr()
    run("account", "list")
    out = capsys.readouterr().out
    assert "BCA Tahapan" in out and "13,000,000" in out and "Overdrawn" not in out


def test_account_errors(run, conn):
    run("account", "add", "Cash", "--type", "cash")
    with pytest.raises(SetupError, match="already exists"):
        run("account", "add", "cash", "--type", "cash")
    with pytest.raises(SetupError, match="No account named"):
        run("account", "set", "Nope", "--type", "bank")
    with pytest.raises(SetupError, match="Nothing to change"):
        run("account", "set", "Cash")
    with pytest.raises(SystemExit):                       # argparse: bad choice
        run("account", "add", "X", "--type", "crypto")


def test_cannot_remove_account_with_transactions(run, conn):
    run("account", "add", "GoPay", "--type", "ewallet")
    conn.execute("INSERT INTO transactions (date, amount, account_id) VALUES ('2026-11-01', 1, 1)")
    conn.commit()
    with pytest.raises(SetupError, match="1 transaction"):
        run("account", "remove", "GoPay")


# ---- debts ---------------------------------------------------------------

def test_debt_add_set_list(run, conn, capsys):
    run("account", "add", "BCA Credit Card", "--type", "credit")
    capsys.readouterr()
    run("debt", "add", "BCA Credit Card", "--balance", "9687566", "--payment", "1134889")
    assert "shares its name" in capsys.readouterr().out
    run("debt", "add", "KPR Mandiri", "--balance", "450jt", "--payment", "5,2jt",
        "--rate", "7.5", "--start", "01/03/2024")
    run("debt", "set", "kpr mandiri", "--balance", "445jt")
    assert _q(conn, "SELECT name, current_balance, monthly_payment, interest_rate, start_date "
                    "FROM debts ORDER BY id") == [
        ("BCA Credit Card", 9_687_566, 1_134_889, None, None),
        ("KPR Mandiri", 445_000_000, 5_200_000, 7.5, "2024-03-01"),
    ]
    capsys.readouterr()
    run("debt", "list")
    assert "Total owed 454,687,566" in capsys.readouterr().out


def test_debt_validation(run):
    with pytest.raises(SetupError, match="--balance"):
        run("debt", "add", "Loan")
    with pytest.raises(SetupError, match="annual percentage"):
        run("debt", "add", "Loan", "--balance", "1jt", "--rate", "750")


# ---- goals ---------------------------------------------------------------

def test_goal_contributions_and_progress(run, conn, capsys):
    run("goal", "add", "Emergency Fund", "--target", "75jt", "--date", "2027-06-01",
        "--priority", "High")
    run("goal", "contribute", "emergency fund", "2jt", "--date", "2026-10-01")
    run("goal", "contribute", "Emergency Fund", "3jt", "--date", "2026-11-01")
    capsys.readouterr()
    run("goal", "contribute", "Emergency Fund", "-1jt", "--date", "2026-11-15",
        "--notes", "car repair")
    assert "Withdrew 1,000,000" in capsys.readouterr().out
    # Snapshot progress uses the same contributions.
    assert gs.goal_progress(conn, "2026-11")["Emergency Fund"]["current"] == 4_000_000
    assert gs.goal_progress(conn, "2026-10")["Emergency Fund"]["current"] == 2_000_000
    capsys.readouterr()
    run("goal", "list")
    assert "5%" in capsys.readouterr().out          # 4jt of 75jt


def test_goal_remove_needs_force_with_contributions(run, conn):
    run("goal", "add", "Liburan", "--target", "10jt")
    run("goal", "contribute", "Liburan", "1jt")
    with pytest.raises(SetupError, match="--force"):
        run("goal", "remove", "Liburan")
    run("goal", "remove", "Liburan", "--force")
    assert _q(conn, "SELECT COUNT(*) FROM goal_contributions") == [(0,)]


def test_goal_set(run, conn):
    run("goal", "add", "Dana Kuliah", "--target", "150jt")
    run("goal", "set", "Dana Kuliah", "--target", "200jt", "--priority", "High",
        "--rename", "Dana Kuliah Anak 1")
    assert _q(conn, "SELECT name, target_amount, priority FROM goals") == [
        ("Dana Kuliah Anak 1", 200_000_000, "High")]


# ---- budgets & categories -----------------------------------------------

def test_budget_range_upsert_and_snapshot(run, conn, capsys):
    run("category", "add", "Household", "--type", "variable")
    run("category", "add", "Groceries", "--type", "variable", "--parent", "Household")
    run("budget", "set", "Groceries", "3jt", "--month", "2026-11", "--through", "2027-01")
    run("budget", "set", "groceries", "3,5jt", "--month", "2026-12")       # overwrite one
    assert _q(conn, "SELECT month, monthly_limit FROM budgets ORDER BY month") == [
        ("2026-11", 3_000_000), ("2026-12", 3_500_000), ("2027-01", 3_000_000)]

    conn.execute("INSERT INTO transactions (date, amount, type, category_id) "
                 "VALUES ('2026-11-05', 3200000, 'expense', 2)")
    conn.commit()
    status = gs.build_snapshot(conn, "2026-11")["budget_status"]["Groceries"]
    assert status == {"actual": 3_200_000, "limit": 3_000_000, "over": True}
    capsys.readouterr()
    run("budget", "list", "--month", "2026-11")
    assert "OVER" in capsys.readouterr().out

    run("budget", "clear", "Groceries", "--month", "2026-12", "--through", "2027-01")
    assert _q(conn, "SELECT COUNT(*) FROM budgets") == [(1,)]


def test_budget_validation(run):
    run("category", "add", "Transport", "--type", "variable")
    with pytest.raises(SetupError, match="Not a month"):
        run("budget", "set", "Transport", "1jt", "--month", "2026-13")
    with pytest.raises(SetupError, match="No category named"):
        run("budget", "set", "Nope", "1jt", "--month", "2026-11")


def test_category_list(run, capsys):
    run("category", "add", "Household", "--type", "variable")
    run("category", "add", "Groceries", "--type", "variable", "--parent", "1")
    capsys.readouterr()
    run("category", "list")
    out = capsys.readouterr().out
    assert out.index("Household") < out.index("  Groceries")   # child under parent


# ---- profile -------------------------------------------------------------

def test_profile_amounts_are_normalised(run, conn, capsys):
    run("profile", "set", "monthly_income_total", "25jt")
    run("profile", "set", "members", "Saya, Istri, Anak 1, Anak 2")
    assert dict(_q(conn, "SELECT key, value FROM family_profile")) == {
        "monthly_income_total": "25000000", "members": "Saya, Istri, Anak 1, Anak 2"}
    assert gs.baseline_income(conn) == 25_000_000
    capsys.readouterr()
    run("profile", "show")
    assert "25,000,000" in capsys.readouterr().out
    run("profile", "unset", "members")
    assert _q(conn, "SELECT key FROM family_profile") == [("monthly_income_total",)]


def test_missing_db(tmp_path):
    with pytest.raises(SetupError, match="init_db"):
        setup.main(["--db", str(tmp_path / "nope.db"), "account", "list"])


@pytest.mark.parametrize("argv,expected", [
    (["account", "add", "X", "--type", "bank", "--balance", "-200rb"], "-200rb"),
    (["account", "set", "X", "--balance", "-1,5jt"], "-1,5jt"),
    (["goal", "contribute", "Fund", "-1jt", "--date", "2026-11-15"], "-1jt"),
])
def test_negative_amounts_are_values_not_options(argv, expected):
    """Regression: Python < 3.14 argparse read '-200rb' as an unknown option.
    The CI matrix runs this on 3.12 as well as 3.14."""
    args = setup.build_parser().parse_args(argv)
    assert expected in (getattr(args, "balance", None), getattr(args, "amount", None))


def test_negative_matcher_applies_to_every_subcommand():
    def walk(parser):
        yield parser
        for action in parser._actions:
            for sub in getattr(action, "choices", None) or {}:
                if isinstance(action.choices, dict):
                    yield from walk(action.choices[sub])
    parsers = list(walk(setup.build_parser()))
    assert len(parsers) > 20
    assert all(p._negative_number_matcher.match("-200rb") for p in parsers)
