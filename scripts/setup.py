#!/usr/bin/env python3
"""Manage reference data: accounts, debts, goals, budgets, categories, profile.

Everything the ingesters don't create — so setting up a real DB never needs
raw SQL. Amounts accept plain numbers, Indonesian formatting, or jt/rb
shorthand: 15000000, "Rp 15.000.000", 15jt, 1,5jt, 750rb.

Usage:
    python scripts/setup.py account add "BCA Checking" --type bank --balance 12.5jt
    python scripts/setup.py account add "BCA Credit Card" --type credit
    python scripts/setup.py account set "GoPay" --balance 350rb
    python scripts/setup.py account list

    python scripts/setup.py debt add "KPR Mandiri" --balance 450jt --payment 5.2jt --rate 7.5
    python scripts/setup.py debt set "BCA Credit Card" --balance 9687566
    python scripts/setup.py debt list

    python scripts/setup.py goal add "Emergency Fund" --target 75jt --date 2027-06-01 --priority High
    python scripts/setup.py goal contribute "Emergency Fund" 2jt --date 2026-11-01
    python scripts/setup.py goal list

    python scripts/setup.py budget set Groceries 3.5jt --month 2026-11 --through 2027-03
    python scripts/setup.py budget list --month 2026-11

    python scripts/setup.py category add "Pets" --type variable --parent Household
    python scripts/setup.py profile set monthly_income_total 25jt
    python scripts/setup.py profile show

All commands take --db (default: data/finance.db).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB, connect                 # noqa: E402
from ingest_sheet import parse_amount, parse_date       # noqa: E402

ACCOUNT_TYPES = ("bank", "cash", "ewallet", "credit", "other")
CATEGORY_TYPES = ("fixed", "variable", "irregular")
PRIORITIES = ("High", "Medium", "Low")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_SHORTHAND = re.compile(
    r"^\s*(-)?\s*(?:rp\.?\s*)?(\d+(?:[.,]\d+)?)\s*(jt|juta|rb|ribu|k)\s*$", re.I)
# After removing an 'Rp' prefix, a plain amount may only hold digits, separators,
# a sign or parentheses — anything else ('5jtt', '2 juta lebih') is a typo, and
# the general parser would silently drop the letters and misread the number.
_PLAIN = re.compile(r"^\s*[-(]?\s*(?:rp\.?\s*)?[\d.,\s]+\)?\s*$", re.I)
_MULTIPLIER = {"jt": 1_000_000, "juta": 1_000_000, "rb": 1_000, "ribu": 1_000, "k": 1_000}


class SetupError(SystemExit):
    """User-facing error: message printed, exit status 1."""


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def parse_idr(raw: str, *, allow_negative: bool = False) -> int:
    """'15jt' / '1,5jt' / '750rb' / 'Rp 15.000.000' / '15000000' -> rupiah."""
    m = _SHORTHAND.match(str(raw))
    if m:
        value = round(float(m.group(2).replace(",", ".")) * _MULTIPLIER[m.group(3).lower()])
        if m.group(1):
            value = -value
    elif _PLAIN.match(str(raw)):
        value = parse_amount(str(raw))
    else:
        value = None
    if value is None:
        raise SetupError(f"Not an amount: {raw!r} (try 15000000, 15jt, 750rb, 'Rp 15.000.000')")
    if value < 0 and not allow_negative:
        raise SetupError(f"Amount must not be negative: {raw!r}")
    return int(value)


def parse_day(raw: str) -> str:
    d = parse_date(str(raw))
    if d is None:
        raise SetupError(f"Not a date: {raw!r} (use YYYY-MM-DD or DD/MM/YYYY)")
    return d


def parse_month(raw: str) -> str:
    if not _MONTH_RE.match(raw):
        raise SetupError(f"Not a month: {raw!r} (use YYYY-MM)")
    return raw


def months_between(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    if (ey, em) < (y, m):
        raise SetupError(f"--through {end} is before --month {start}")
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _fmt(n) -> str:
    return "-" if n is None else f"{int(n):,}"


def _table(headers: list[str], rows: list[tuple], right: set[int] = frozenset()) -> None:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = lambda vals: "  ".join(
        v.rjust(w) if i in right else v.ljust(w) for i, (v, w) in enumerate(zip(vals, widths)))
    print(line(headers))
    print(line(["-" * w for w in widths]))
    for r in cells:
        print(line(r))


# --------------------------------------------------------------------------
# Lookups (case-insensitive by name)
# --------------------------------------------------------------------------

_SINGULAR = {"accounts": "account", "debts": "debt", "goals": "goal",
             "categories": "category"}


def _get(conn, table: str, name: str, cols: str = "id, name"):
    row = conn.execute(f"SELECT {cols} FROM {table} WHERE name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    if row is None:
        known = [r[0] for r in conn.execute(f"SELECT name FROM {table} ORDER BY name")]
        raise SetupError(f"No {_SINGULAR[table]} named {name!r}. "
                         f"Known: {known or 'none yet'}")
    return row


def _exists(conn, table: str, name: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {table} WHERE name = ? COLLATE NOCASE",
                        (name,)).fetchone() is not None


# --------------------------------------------------------------------------
# account
# --------------------------------------------------------------------------

def account_add(conn, a) -> None:
    if _exists(conn, "accounts", a.name):
        raise SetupError(f"Account {a.name!r} already exists (use 'account set').")
    bal = parse_idr(a.balance, allow_negative=True) if a.balance else 0
    conn.execute("INSERT INTO accounts (name, type, starting_balance) VALUES (?,?,?)",
                 (a.name, a.type, bal))
    print(f"Added account {a.name!r} ({a.type}), starting balance {bal:,}.")
    if a.type == "credit" and bal > 0:
        print("  note: a card's starting balance is usually 0 or negative (money owed).")


def account_set(conn, a) -> None:
    aid, name = _get(conn, "accounts", a.name)
    changes = []
    if a.type:
        conn.execute("UPDATE accounts SET type=? WHERE id=?", (a.type, aid))
        changes.append(f"type={a.type}")
    if a.balance is not None:
        bal = parse_idr(a.balance, allow_negative=True)
        conn.execute("UPDATE accounts SET starting_balance=? WHERE id=?", (bal, aid))
        changes.append(f"starting balance={bal:,}")
    if a.rename:
        if _exists(conn, "accounts", a.rename) and a.rename.lower() != name.lower():
            raise SetupError(f"Account {a.rename!r} already exists.")
        conn.execute("UPDATE accounts SET name=? WHERE id=?", (a.rename, aid))
        changes.append(f"renamed to {a.rename!r}")
    if not changes:
        raise SetupError("Nothing to change: pass --type, --balance or --rename.")
    print(f"Updated account {name!r}: {', '.join(changes)}.")


def account_remove(conn, a) -> None:
    aid, name = _get(conn, "accounts", a.name)
    used = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (aid,)).fetchone()[0]
    if used:
        raise SetupError(f"Account {name!r} has {used} transaction(s); not removing it.")
    conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
    print(f"Removed account {name!r}.")


def account_list(conn, a) -> None:
    rows = conn.execute(
        "SELECT a.name, a.type, a.starting_balance, COUNT(t.id), MIN(t.date), MAX(t.date) "
        "FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id "
        "GROUP BY a.id ORDER BY a.type, a.name").fetchall()
    if not rows:
        print("No accounts yet. Add one with: setup.py account add NAME --type bank")
        return
    _table(["account", "type", "starting balance", "txns", "first", "last"],
           [(n, t, _fmt(b), c, f or "-", l or "-") for n, t, b, c, f, l in rows], {2, 3})


# --------------------------------------------------------------------------
# debt
# --------------------------------------------------------------------------

def _debt_values(a) -> dict:
    vals = {}
    if a.balance is not None:
        vals["current_balance"] = parse_idr(a.balance)
    if a.payment is not None:
        vals["monthly_payment"] = parse_idr(a.payment)
    if a.principal is not None:
        vals["principal"] = parse_idr(a.principal)
    if a.rate is not None:
        if not 0 <= a.rate <= 100:
            raise SetupError(f"--rate is an annual percentage (e.g. 7.5), got {a.rate}")
        vals["interest_rate"] = a.rate
    if a.start is not None:
        vals["start_date"] = parse_day(a.start)
    if a.notes is not None:
        vals["notes"] = a.notes
    return vals


def _card_hint(conn, name: str) -> None:
    row = conn.execute("SELECT type FROM accounts WHERE name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    if row:
        print(f"  this debt shares its name with account {name!r}: net worth counts the "
              "debt balance and skips that account's own ledger (no double counting).")


def debt_add(conn, a) -> None:
    if conn.execute("SELECT 1 FROM debts WHERE name = ? COLLATE NOCASE", (a.name,)).fetchone():
        raise SetupError(f"Debt {a.name!r} already exists (use 'debt set').")
    if a.balance is None:
        raise SetupError("debt add needs --balance (the amount currently owed).")
    vals = _debt_values(a)
    cols = ["name"] + list(vals)
    conn.execute(f"INSERT INTO debts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [a.name] + list(vals.values()))
    print(f"Added debt {a.name!r}: balance {vals['current_balance']:,}"
          + (f", payment {vals['monthly_payment']:,}/month" if "monthly_payment" in vals else "")
          + ".")
    _card_hint(conn, a.name)


def debt_set(conn, a) -> None:
    did, name = _get(conn, "debts", a.name)
    vals = _debt_values(a)
    if not vals:
        raise SetupError("Nothing to change: pass --balance, --payment, --rate, "
                         "--principal, --start or --notes.")
    conn.execute(f"UPDATE debts SET {', '.join(f'{c}=?' for c in vals)} WHERE id=?",
                 list(vals.values()) + [did])
    print(f"Updated debt {name!r}: " + ", ".join(
        f"{k}={_fmt(v) if isinstance(v, int) else v}" for k, v in vals.items()) + ".")


def debt_remove(conn, a) -> None:
    did, name = _get(conn, "debts", a.name)
    conn.execute("DELETE FROM debts WHERE id=?", (did,))
    print(f"Removed debt {name!r}.")


def debt_list(conn, a) -> None:
    rows = conn.execute("SELECT name, current_balance, monthly_payment, interest_rate, "
                        "start_date FROM debts ORDER BY current_balance DESC").fetchall()
    if not rows:
        print("No debts recorded.")
        return
    _table(["debt", "balance", "payment/month", "rate %", "since"],
           [(n, _fmt(b), _fmt(p), "-" if r is None else f"{r:g}", s or "-")
            for n, b, p, r, s in rows], {1, 2, 3})
    total = sum(r[1] for r in rows)
    pay = sum(r[2] or 0 for r in rows)
    print(f"\nTotal owed {total:,}; payments {pay:,}/month.")


# --------------------------------------------------------------------------
# goal
# --------------------------------------------------------------------------

def goal_add(conn, a) -> None:
    if _exists(conn, "goals", a.name):
        raise SetupError(f"Goal {a.name!r} already exists (use 'goal set').")
    target = parse_idr(a.target)
    if target == 0:
        raise SetupError("--target must be more than 0.")
    date = parse_day(a.date) if a.date else None
    conn.execute("INSERT INTO goals (name, target_amount, target_date, priority) VALUES (?,?,?,?)",
                 (a.name, target, date, a.priority))
    print(f"Added goal {a.name!r}: {target:,} by {date or 'no date'} ({a.priority}).")


def goal_set(conn, a) -> None:
    gid, name = _get(conn, "goals", a.name)
    vals = {}
    if a.target is not None:
        vals["target_amount"] = parse_idr(a.target)
    if a.date is not None:
        vals["target_date"] = parse_day(a.date)
    if a.priority is not None:
        vals["priority"] = a.priority
    if a.rename:
        vals["name"] = a.rename
    if not vals:
        raise SetupError("Nothing to change: pass --target, --date, --priority or --rename.")
    conn.execute(f"UPDATE goals SET {', '.join(f'{c}=?' for c in vals)} WHERE id=?",
                 list(vals.values()) + [gid])
    print(f"Updated goal {name!r}.")


def goal_contribute(conn, a) -> None:
    gid, name = _get(conn, "goals", a.name)
    amount = parse_idr(a.amount, allow_negative=True)   # negative = withdrawal
    if amount == 0:
        raise SetupError("Contribution must not be 0.")
    from datetime import date as _date
    day = parse_day(a.date) if a.date else _date.today().isoformat()
    conn.execute("INSERT INTO goal_contributions (goal_id, date, amount, source, notes) "
                 "VALUES (?,?,?,?,?)", (gid, day, amount, a.source, a.notes))
    total, target = conn.execute(
        "SELECT COALESCE(SUM(gc.amount),0), g.target_amount FROM goals g "
        "LEFT JOIN goal_contributions gc ON gc.goal_id=g.id WHERE g.id=?", (gid,)).fetchone()
    verb = "Added" if amount > 0 else "Withdrew"
    print(f"{verb} {abs(amount):,} {'to' if amount > 0 else 'from'} {name!r} on {day}. "
          f"Now {total:,} of {target:,} ({total / target:.0%}).")


def goal_remove(conn, a) -> None:
    gid, name = _get(conn, "goals", a.name)
    n = conn.execute("SELECT COUNT(*) FROM goal_contributions WHERE goal_id=?", (gid,)).fetchone()[0]
    if n and not a.force:
        raise SetupError(f"Goal {name!r} has {n} contribution(s); pass --force to delete them too.")
    conn.execute("DELETE FROM goals WHERE id=?", (gid,))   # contributions cascade
    print(f"Removed goal {name!r}" + (f" and {n} contribution(s)." if n else "."))


def goal_list(conn, a) -> None:
    rows = conn.execute(
        "SELECT g.name, g.priority, g.target_amount, COALESCE(SUM(gc.amount),0), g.target_date "
        "FROM goals g LEFT JOIN goal_contributions gc ON gc.goal_id=g.id "
        "GROUP BY g.id ORDER BY CASE g.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 "
        "ELSE 2 END, g.target_date").fetchall()
    if not rows:
        print("No goals yet. Add one with: setup.py goal add NAME --target 75jt")
        return
    _table(["goal", "priority", "target", "saved", "%", "by"],
           [(n, p, _fmt(t), _fmt(s), f"{s / t:.0%}", d or "-") for n, p, t, s, d in rows],
           {2, 3, 4})


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------

def _category(conn, ref: str) -> tuple[int, str]:
    row = None
    if ref.isdigit():
        row = conn.execute("SELECT id, name FROM categories WHERE id=?", (int(ref),)).fetchone()
    return row or _get(conn, "categories", ref)


def budget_set(conn, a) -> None:
    cid, cname = _category(conn, a.category)
    limit = parse_idr(a.limit)
    start = parse_month(a.month)
    months = months_between(start, parse_month(a.through)) if a.through else [start]
    conn.executemany(
        "INSERT INTO budgets (category_id, month, monthly_limit) VALUES (?,?,?) "
        "ON CONFLICT(category_id, month) DO UPDATE SET monthly_limit=excluded.monthly_limit",
        [(cid, m, limit) for m in months])
    span = months[0] if len(months) == 1 else f"{months[0]}..{months[-1]} ({len(months)} months)"
    print(f"Budget {cname!r}: {limit:,}/month for {span}.")


def budget_clear(conn, a) -> None:
    cid, cname = _category(conn, a.category)
    start = parse_month(a.month)
    months = months_between(start, parse_month(a.through)) if a.through else [start]
    cur = conn.execute(
        f"DELETE FROM budgets WHERE category_id=? AND month IN ({','.join('?' * len(months))})",
        [cid] + months)
    print(f"Cleared {cur.rowcount} budget month(s) for {cname!r}.")


def budget_list(conn, a) -> None:
    q = ("SELECT b.month, c.name, b.monthly_limit, "
         "  (SELECT COALESCE(SUM(t.amount),0) FROM transactions t "
         "   WHERE t.category_id=b.category_id AND t.type='expense' "
         "     AND substr(t.date,1,7)=b.month) "
         "FROM budgets b JOIN categories c ON c.id=b.category_id")
    params: tuple = ()
    if a.month:
        q += " WHERE b.month=?"
        params = (parse_month(a.month),)
    rows = conn.execute(q + " ORDER BY b.month, c.name", params).fetchall()
    if not rows:
        print("No budgets set" + (f" for {a.month}." if a.month else "."))
        return
    _table(["month", "category", "limit", "spent", "left"],
           [(m, n, _fmt(l), _fmt(s), _fmt(l - s) + (" OVER" if s > l else ""))
            for m, n, l, s in rows], {2, 3, 4})


# --------------------------------------------------------------------------
# category
# --------------------------------------------------------------------------

def category_add(conn, a) -> None:
    if _exists(conn, "categories", a.name):
        raise SetupError(f"Category {a.name!r} already exists.")
    parent = _category(conn, a.parent)[0] if a.parent else None
    conn.execute("INSERT INTO categories (name, type, parent_id) VALUES (?,?,?)",
                 (a.name, a.type, parent))
    print(f"Added category {a.name!r} ({a.type})" + (f" under {a.parent!r}." if a.parent else "."))


def category_list(conn, a) -> None:
    rows = conn.execute(
        "SELECT c.id, c.name, c.type, p.name, "
        "  (SELECT COUNT(*) FROM transactions t WHERE t.category_id=c.id) "
        "FROM categories c LEFT JOIN categories p ON p.id=c.parent_id "
        "ORDER BY COALESCE(p.name, c.name), p.name IS NOT NULL, c.name").fetchall()
    if not rows:
        print("No categories yet.")
        return
    _table(["id", "category", "type", "parent", "txns"],
           [(i, ("  " if p else "") + n, t, p or "-", c) for i, n, t, p, c in rows], {0, 4})


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------

_AMOUNT_KEYS = {"monthly_income_total", "fixed_obligations_total"}


def profile_set(conn, a) -> None:
    value = str(parse_idr(a.value)) if a.key in _AMOUNT_KEYS else a.value
    conn.execute("INSERT INTO family_profile (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (a.key, value))
    shown = f"{int(value):,}" if a.key in _AMOUNT_KEYS else value
    print(f"Set {a.key} = {shown}.")
    if a.key == "monthly_income_total":
        print("  used as snapshot income only for months with no income recorded "
              "(income_basis 'baseline'). Re-run generate_snapshot.py --all.")


def profile_unset(conn, a) -> None:
    cur = conn.execute("DELETE FROM family_profile WHERE key=?", (a.key,))
    print(f"Removed {a.key}." if cur.rowcount else f"{a.key} was not set.")


def profile_show(conn, a) -> None:
    rows = conn.execute("SELECT key, value FROM family_profile ORDER BY key").fetchall()
    if not rows:
        print("Profile is empty. Start with: setup.py profile set monthly_income_total 25jt")
        return
    _table(["key", "value"],
           [(k, f"{int(v):,}" if k in _AMOUNT_KEYS and v.isdigit() else v) for k, v in rows])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage accounts, debts, goals, budgets, "
                                "categories and the household profile.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"SQLite DB path (default: {DEFAULT_DB})")
    top = p.add_subparsers(dest="group", required=True)

    def group(name, help_):
        g = top.add_parser(name, help=help_)
        return g.add_subparsers(dest="action", required=True)

    def cmd(sub, name, fn, help_):
        c = sub.add_parser(name, help=help_)
        c.set_defaults(fn=fn)
        return c

    acc = group("account", "Bank / cash / e-wallet / credit-card accounts")
    c = cmd(acc, "add", account_add, "Add an account")
    c.add_argument("name")
    c.add_argument("--type", required=True, choices=ACCOUNT_TYPES)
    c.add_argument("--balance", help="Balance on the day before your first imported "
                   "transaction (default 0; negative allowed)")
    c = cmd(acc, "set", account_set, "Change an account")
    c.add_argument("name")
    c.add_argument("--type", choices=ACCOUNT_TYPES)
    c.add_argument("--balance")
    c.add_argument("--rename")
    c = cmd(acc, "remove", account_remove, "Remove an unused account")
    c.add_argument("name")
    cmd(acc, "list", account_list, "List accounts")

    debt = group("debt", "Loans, cards and other liabilities")
    for name, fn in (("add", debt_add), ("set", debt_set)):
        c = cmd(debt, name, fn, f"{name.title()} a debt")
        c.add_argument("name")
        c.add_argument("--balance", help="Amount currently owed")
        c.add_argument("--payment", help="Monthly payment (used for debt-to-income)")
        c.add_argument("--rate", type=float, help="Annual interest rate, %%")
        c.add_argument("--principal", help="Original amount borrowed")
        c.add_argument("--start", help="Start date")
        c.add_argument("--notes")
    c = cmd(debt, "remove", debt_remove, "Remove a debt")
    c.add_argument("name")
    cmd(debt, "list", debt_list, "List debts")

    goal = group("goal", "Savings goals and contributions")
    c = cmd(goal, "add", goal_add, "Add a goal")
    c.add_argument("name")
    c.add_argument("--target", required=True)
    c.add_argument("--date", help="Target date")
    c.add_argument("--priority", choices=PRIORITIES, default="Medium")
    c = cmd(goal, "set", goal_set, "Change a goal")
    c.add_argument("name")
    c.add_argument("--target")
    c.add_argument("--date")
    c.add_argument("--priority", choices=PRIORITIES)
    c.add_argument("--rename")
    c = cmd(goal, "contribute", goal_contribute, "Record money put toward (or taken from) a goal")
    c.add_argument("name")
    c.add_argument("amount", help="Negative for a withdrawal")
    c.add_argument("--date", help="Default: today")
    c.add_argument("--source")
    c.add_argument("--notes")
    c = cmd(goal, "remove", goal_remove, "Remove a goal")
    c.add_argument("name")
    c.add_argument("--force", action="store_true", help="Also delete its contributions")
    cmd(goal, "list", goal_list, "List goals with progress")

    bud = group("budget", "Monthly spending limits per category")
    c = cmd(bud, "set", budget_set, "Set a monthly limit")
    c.add_argument("category", help="Category name or id")
    c.add_argument("limit")
    c.add_argument("--month", required=True, help="YYYY-MM")
    c.add_argument("--through", help="Apply to every month up to this YYYY-MM")
    c = cmd(bud, "clear", budget_clear, "Remove a limit")
    c.add_argument("category")
    c.add_argument("--month", required=True)
    c.add_argument("--through")
    c = cmd(bud, "list", budget_list, "List limits with spending so far")
    c.add_argument("--month")

    cat = group("category", "Spending categories")
    c = cmd(cat, "add", category_add, "Add a category")
    c.add_argument("name")
    c.add_argument("--type", required=True, choices=CATEGORY_TYPES)
    c.add_argument("--parent", help="Parent category name or id")
    cmd(cat, "list", category_list, "List categories")

    prof = group("profile", "Household profile (key/value)")
    c = cmd(prof, "set", profile_set, "Set a value")
    c.add_argument("key", help="e.g. monthly_income_total, household_size, members")
    c.add_argument("value")
    c = cmd(prof, "unset", profile_unset, "Remove a value")
    c.add_argument("key")
    cmd(prof, "show", profile_show, "Show the profile")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not args.db.exists():
        raise SetupError(f"DB not found: {args.db}. Run init_db.py first.")
    conn = connect(args.db)
    try:
        args.fn(conn, args)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise SetupError(f"Database rejected the change: {e}") from e
    finally:
        conn.close()


if __name__ == "__main__":
    main()
