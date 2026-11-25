#!/usr/bin/env python3
"""Decide whether a statement line is spending, income, or a transfer.

Shared by the PDF and sheet importers so both apply identical rules.

A TRANSFER is money moving between the household's own accounts: it is
neither income nor spending. Getting this wrong in either direction skews the
numbers:
  - a card bill payment counted as spending double-counts every card charge;
  - but a top-up to an e-wallet you never import IS the only record of that
    spending, so calling it a transfer would make it vanish.
So transfer rules depend on what you actually track (Context): card payments
are transfers only if a 'credit' account exists, e-wallet top-ups only if that
wallet is one of your accounts, and money to/from your own name only if
`owner_names` is set in the profile.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

DIRECTIONS = ("expense", "income", "transfer")

# Credit markers -> money coming in. Deliberately strict: a standalone 'CR'
# flag (not the substring in 'CREDIT'), or explicit refund-type terms.
_CREDIT_RE = re.compile(r"\bCR\b|\brefund\b|\bcashback\b|\breversal\b", re.I)

# Card bill payment, as written on either statement.
_CARD_PAYMENT_RE = re.compile(
    r"\b(?:pembayaran|pemb\.?|bayar|byr|payment)\W*"
    r"(?:kartu\s*kredit|credit\s*card|kk|cc)\b"        # bank side
    r"|\b(?:pembayaran|payment)\W+(?:-\s*)?(?:terima\s*kasih|thank\s*you)",  # card side
    re.I)
# On a card statement, any 'payment' line is the bill being paid.
_PAYMENT_WORD_RE = re.compile(r"\b(?:pembayaran|payment)\b", re.I)
# On a bank statement, a card mentioned without a fee word is a bill payment
# (BCA: 'KARTU KREDIT/PL ... BCA CARD').
_CARD_MENTION_RE = re.compile(r"kartu\s*kredit|credit\s*card", re.I)
_FEE_RE = re.compile(r"\b(?:biaya|fee|iuran|annual|bunga|interest|denda)\b", re.I)

# E-wallet top-up: an explicit top-up word, or a payment into the wallet's
# virtual account (BCA: 'TRSF E-BANKING DB .../FTFVA/... 12208/SHOPEEPAY').
# 'TOP UP PULSA' (phone credit) names no wallet, so it stays spending.
_TOPUP_RE = re.compile(r"\btop[\s-]?up\b|topup|\bftfva\b", re.I)
# Letter-only boundaries: BCA glues wallet names to digits and slashes
# ('70001/GOPAY', '12208/SHOPEEPAY'), but 'NOVOTEL' must not read as OVO.
_EWALLET_RE = re.compile(
    r"(?<![a-z])(gopay|go-pay|ovo|dana|shopee\s*pay|linkaja|link\s*aja|isaku|flip)(?![a-z])",
    re.I)

# A line that moves money by bank transfer (needed before an owner-name match
# counts: a QR payment to a shop that shares your name is still spending).
_TRANSFER_WORD_RE = re.compile(r"\b(?:transfer|trsf|trf|bi-fast|switching)\b", re.I)


def _wallet_key(name: str) -> str:
    return re.sub(r"[\s-]", "", name.lower())


@dataclass(frozen=True)
class Context:
    """What the household tracks. The defaults (used when no DB is at hand,
    e.g. in unit tests) assume everything is tracked: the pre-Context rules."""
    has_credit_account: bool = True
    ewallets: frozenset[str] | None = None        # wallet keys; None = any
    owner_names: tuple[str, ...] = ()

    def tracks_wallet(self, line: str) -> bool:
        found = {_wallet_key(m) for m in _EWALLET_RE.findall(line)}
        if not found:
            return False
        if self.ewallets is None:
            return True
        return any(f in w or w in f for f in found for w in self.ewallets)

    def mentions_owner(self, line: str) -> bool:
        low = re.sub(r"\s+", " ", line.lower())
        return any(n in low for n in self.owner_names)


def load_context(conn: sqlite3.Connection) -> Context:
    accounts = conn.execute("SELECT name, type FROM accounts").fetchall()
    wallets = {_wallet_key(n) for n, t in accounts if t == "ewallet"}
    row = conn.execute(
        "SELECT value FROM family_profile WHERE key='owner_names'").fetchone()
    owners = tuple(re.sub(r"\s+", " ", n.strip().lower())
                   for n in (row[0].split(",") if row else []) if n.strip())
    return Context(
        has_credit_account=any(t == "credit" for _, t in accounts),
        ewallets=frozenset(wallets),
        owner_names=owners,
    )


def is_transfer(line: str, source_type: str | None = None,
                ctx: Context | None = None) -> bool:
    """True when the line moves money between the household's own accounts."""
    ctx = ctx or Context()
    if source_type == "cc" and (_CARD_PAYMENT_RE.search(line)
                                or _PAYMENT_WORD_RE.search(line)):
        return True                      # paying the card we are reading
    if _CARD_PAYMENT_RE.search(line) or (
            source_type == "bank" and _CARD_MENTION_RE.search(line)
            and not _FEE_RE.search(line)):
        return ctx.has_credit_account
    if _TOPUP_RE.search(line):
        if source_type == "ewallet":
            return True                  # the wallet we are reading was topped up
        if ctx.tracks_wallet(line):
            return True
    # Own-name transfer, but not its fee ('BI-FAST DB / BIAYA TXN KE 008 <owner>'):
    # the fee is money really paid to the bank.
    if (ctx.owner_names and _TRANSFER_WORD_RE.search(line)
            and ctx.mentions_owner(line) and not _FEE_RE.search(line)):
        return True
    return False


def classify_direction(line: str, source_type: str | None = None,
                       ctx: Context | None = None) -> str:
    """'transfer', 'income' (credit markers) or 'expense' for one statement line."""
    if is_transfer(line, source_type, ctx):
        return "transfer"
    if _CREDIT_RE.search(line):
        return "income"
    return "expense"
