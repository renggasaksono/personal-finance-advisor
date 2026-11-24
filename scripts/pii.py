#!/usr/bin/env python3
"""Redact personal information from statement text before it is sent or stored.

Deterministic, offline scrubbing of the structured PII that appears on
Indonesian bank / credit-card / e-wallet statements:

    emails, phone numbers, NPWP (tax id), card numbers (incl. 4-4-4-4 groups
    and Luhn-valid runs), and long bare account numbers.

IDR amounts are protected: money is written with '.'/','/space separators or an
'Rp' prefix, so properly-formatted amounts never match the digit rules here.

This is what makes `ingest_pdf.py --ai` safe: the extracted text is redacted
before it goes to Claude, and parsed descriptions are redacted before they are
written to the local DB. Names and street addresses are only partially handled
(see redact_names) — structured identifiers are the reliable, high-value target.

CLI preview:
    python scripts/pii.py path/to/statement.pdf      # or a .txt file
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# --- patterns -------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# NPWP (old 15-digit dotted form). 16-digit NIK-based NPWP is caught by the
# long-digit rule below.
_NPWP = re.compile(r"\b\d{2}\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}\b")
# Indonesian mobile: 08xxxxxxxx / +628xxxxxxxx / 628xxxxxxxx
_PHONE = re.compile(r"(?<!\d)(?:\+?62|0)8\d[\d\s-]{6,13}\d(?!\d)")
# Card printed in groups of 4 (13-19 digits total). The trailing lookahead stops
# the match from hopping a space into an adjacent amount: without it,
# '4111 1111 1111 1111 1.250.000' swallowed the amount's leading '1'.
_CARD_GROUPS = re.compile(r"\b(?:\d[ -]?){12,18}\d\b(?![.,]\d)")
# A bare run of >=10 digits with no separators on either side (account/card).
# The (?<![\d.,]) / (?![\d.,]) guards keep separator-formatted amounts safe.
_LONG_DIGITS = re.compile(r"(?<![\d.,])\d{10,}(?![\d.,])")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> tuple[str, Counter]:
    """Return (redacted_text, counts_by_type). Order matters (specific first)."""
    counts: Counter = Counter()

    def sub(pattern, label, s, *, guard=None):
        def repl(m):
            if guard and not guard(m):
                return m.group(0)
            counts[label] += 1
            return f"[{label}]"
        return pattern.sub(repl, s)

    text = sub(_EMAIL, "EMAIL", text)
    text = sub(_NPWP, "NPWP", text)
    text = sub(_PHONE, "PHONE", text)

    # Card groups: only redact if the digits (minus separators) are 13-19 long.
    def _card_guard(m):
        digits = re.sub(r"\D", "", m.group(0))
        return 13 <= len(digits) <= 19
    text = sub(_CARD_GROUPS, "CARD", text, guard=_card_guard)

    # Bare long digit runs: skip if preceded by an 'Rp' currency marker.
    def _long_guard(m):
        prefix = m.string[max(0, m.start() - 4):m.start()].lower()
        if "rp" in prefix:
            return False
        digits = m.group(0)
        # 13-19 digits: treat as card only if Luhn-valid, else still an account.
        return True
    def _long_label(m):
        digits = m.group(0)
        return "CARD" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else "ACCT"

    def _long_repl(m):
        if not _long_guard(m):
            return m.group(0)
        label = _long_label(m)
        counts[label] += 1
        return f"[{label}]"
    text = _LONG_DIGITS.sub(_long_repl, text)

    return text, counts


# Common Indonesian address / label lead-ins for best-effort line handling.
# No blanket trailing \b: it can't follow a '.' + space ('Kel. Menteng') or sit
# between digits ('RT 002'), which silently disabled those alternatives.
_ADDR_HINT = re.compile(
    r"\b(?:jl\b|jalan\b|kel\.|kec\.|rt\.?\s?\d+\b|rw\.?\s?\d+\b|kode\s?pos\b)", re.I)


def redact_names(text: str) -> tuple[str, int]:
    """Best-effort: mask lines that look like street addresses. Names are NOT
    reliably detectable without NER; callers should treat this as a coarse pass."""
    out, n = [], 0
    for line in text.splitlines():
        if _ADDR_HINT.search(line):
            out.append("[ADDRESS]")
            n += 1
        else:
            out.append(line)
    return "\n".join(out), n


def redact_field(value: str | None) -> str | None:
    """Redact a single stored field (e.g. a transaction description)."""
    if not value:
        return value
    cleaned, _ = redact(value)
    return cleaned


def _main() -> None:
    ap = argparse.ArgumentParser(description="Preview PII redaction on a file.")
    ap.add_argument("file", type=Path, help=".txt or .pdf to preview")
    ap.add_argument("--addresses", action="store_true",
                    help="Also mask address-looking lines (coarse)")
    args = ap.parse_args()
    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file}")

    if args.file.suffix.lower() == ".pdf":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ingest_pdf import extract_text
        text, method = extract_text(args.file)
        print(f"[extracted via {method}]")
    else:
        text = args.file.read_text(encoding="utf-8", errors="replace")

    cleaned, counts = redact(text)
    if args.addresses:
        cleaned, addr_n = redact_names(cleaned)
        if addr_n:
            counts["ADDRESS"] = addr_n
    print("--- redacted text ---")
    print(cleaned)
    print("\n--- removed ---")
    print(dict(counts) or "nothing matched")


if __name__ == "__main__":
    _main()
