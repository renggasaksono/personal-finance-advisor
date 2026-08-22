#!/usr/bin/env python3
"""Shared ingestion core: dedup + the one place transactions are written.

Every ingester (sheet, receipt, PDF statement) produces rows in the same
canonical order and hands them to commit_transactions(), so deduplication and
insertion behave identically regardless of source. This is what lets one
`ingest.py` entry point cover every file type.

Canonical row order (matches the transactions columns we set):
    (date, amount, type, category_id, merchant, account_id,
     family_member, source, notes[, external_ref])
external_ref is optional (statements may carry a bank-provided txn id).

Deduplication:
    - If a row has an external_ref, the dedup key is (account, external_ref) —
      the bank's own id is the strongest signal.
    - Otherwise the key is (date, |amount|, type, normalized-merchant, account)
      plus an occurrence index so N genuinely-identical rows in one batch are
      kept, while re-importing an overlapping statement collapses to the same
      hashes and is ignored.
Insertion uses INSERT OR IGNORE against a UNIQUE index on dedup_hash.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3

VALID_TYPES = {"expense", "income", "transfer"}


def normalize_merchant(merchant: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for stable hashing."""
    if not merchant:
        return ""
    s = re.sub(r"[^\w\s]", " ", str(merchant).lower())
    return re.sub(r"\s+", " ", s).strip()


def dedup_hash(date: str, amount: int, ttype: str, merchant: str | None,
               account_id, external_ref: str | None, occurrence: int) -> str:
    if external_ref:
        base = f"ref|{account_id or ''}|{external_ref}"
    else:
        base = (f"{date}|{abs(int(amount))}|{ttype}|"
                f"{normalize_merchant(merchant)}|{account_id or ''}|{occurrence}")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _prepare(rows) -> list[tuple]:
    """Attach a dedup_hash to each row, handling within-batch occurrences."""
    seen: dict[tuple, int] = {}
    prepared = []
    for r in rows:
        date, amount, ttype, cat, merch, acct, fam, source, notes, *rest = r
        ext = rest[0] if rest else None
        amount = abs(int(amount))
        if ext:
            h = dedup_hash(date, amount, ttype, merch, acct, ext, 0)
        else:
            key = (date, amount, ttype, normalize_merchant(merch), acct)
            occ = seen.get(key, 0)
            seen[key] = occ + 1
            h = dedup_hash(date, amount, ttype, merch, acct, None, occ)
        prepared.append((date, amount, ttype, cat, merch, acct, fam,
                         source, notes, ext, h))
    return prepared


def commit_transactions(conn: sqlite3.Connection, rows) -> tuple[int, int]:
    """Insert rows with dedup. Returns (inserted, duplicates_skipped)."""
    prepared = _prepare(rows)
    if not prepared:
        return (0, 0)
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO transactions "
        "(date, amount, type, category_id, merchant, account_id, "
        " family_member, source, notes, external_ref, dedup_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        prepared,
    )
    conn.commit()
    inserted = conn.total_changes - before
    return (inserted, len(prepared) - inserted)
