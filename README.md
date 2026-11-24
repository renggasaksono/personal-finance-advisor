# Personal Finance Advisor

A privacy-first, Indonesia-localized personal finance tracker and AI-assisted advisor.

- Raw financial data stays local (SQLite)
- Only aggregated monthly snapshots are shared with Claude for analysis
- Ingests expense sheets, receipt photos, and PDF statements (credit card,
  e-wallet, bank) — no direct bank account linking
- Duplicate statements/rows are detected and skipped automatically

See `docs/PLANNING.md` for architecture and `docs/finance_db_schema.md` for the full schema with sample data.

## Status
Core pipeline complete. Ingestion (sheets, receipts, PDF statements),
deduplication, categorization, snapshots, reconciliation, Dropbox sync, and
Claude export are all in `scripts/`.

## Setup
```bash
pip install -r requirements.txt             # pandas/openpyxl/pillow/pytesseract/pdfplumber/...
python scripts/init_db.py --reset --seed    # fresh DB (data/finance.db) with sample data
# upgrading an existing DB created before dedup/statements:
python scripts/migrate.py
```
### Tests
```bash
pip install -r requirements-dev.txt
pytest
```
Every test uses a throwaway SQLite DB in a temp dir — nothing touches
`data/finance.db`, your `.env`, the network, or Tesseract (OCR and PDF text
extraction are stubbed). Known bugs are pinned as `xfail(strict=True)` tests,
so fixing one makes the suite tell you to drop the marker.

Receipt/scanned-PDF OCR also needs the Tesseract binary
(`brew install tesseract tesseract-lang` on macOS). The `--ai` PDF parser and
`export_analysis.py` are the only features that can talk to the network, and
only when you explicitly invoke them.

## Workflow
```bash
# 1. Create / reset the local DB (source of truth, git-ignored)
python scripts/init_db.py --reset --seed

# 2. Ingest expense data — one command auto-routes by file type
python scripts/ingest.py data/inbox/ --account "BCA Checking" --dry-run  # preview
python scripts/ingest.py data/inbox/ --account "BCA Checking"
#   CSV/XLSX -> sheet parser | images -> receipt OCR | PDF -> statement parser
# PDF statements (local parser is default; --ai sends extracted TEXT to Claude):
python scripts/ingest.py data/statements/ --type cc --account "BCA Card"
python scripts/ingest.py data/statements/hard.pdf --type ewallet --ai
# Password-protected statements: you'll be prompted securely, or:
python scripts/ingest.py data/statements/bca.pdf --type cc --password '...'  # or PFA_PDF_PASSWORD in .env

# 2b. Reconcile fuzzy cross-source duplicates (e.g. receipt + card statement)
python scripts/reconcile.py --cross-only
python scripts/reconcile.py --drop 42        # remove one reviewed row by id

# 3. Categorize unknown merchants (learning loop feeds future ingests)
python scripts/categorize.py list
python scripts/categorize.py set "Warung Tegal" "Household"
python scripts/categorize.py review          # interactive
python scripts/categorize.py stats

# 4. Roll up into monthly snapshots (the only data shared externally)
python scripts/generate_snapshot.py --all
python scripts/generate_snapshot.py 2026-08 --show   # inspect before sharing

# 5. Export a paste-ready Claude analysis prompt (aggregates only)
python scripts/export_analysis.py            # -> data/analysis_prompt.md
```

### Optional: pull from Dropbox
Instead of local files, ingest straight from a Dropbox folder. Files are
downloaded to a temp dir, ingested, then deleted — receipt images are never
retained locally. Copy `.env.example` to `.env` and add your Dropbox app
credentials first.
```bash
python scripts/dropbox_sync.py list --folder /Finance/Inbox
python scripts/dropbox_sync.py pull --folder /Finance/Inbox --dry-run
python scripts/dropbox_sync.py pull --folder /Finance/Inbox --account "BCA Checking" \
    --type cc --archive /Finance/Done        # sheets + images + PDFs, then archive
# granular commands (pull-receipts / pull-sheets) still exist if you want them.
```

## Scripts
| Script | Purpose |
|--------|---------|
| `init_db.py` | Create the SQLite schema; `--seed` loads sample data |
| `migrate.py` | Upgrade an existing DB to the current schema (dedup + statements) |
| `ingest.py` | **Unified entry point** — auto-routes any file/folder by type |
| `ingest_core.py` | Shared dedup + insert (one place all ingesters write through) |
| `ingest_sheet.py` | CSV/XLSX → `transactions` (pandas, IDR/date parsing) |
| `ingest_receipt.py` | Receipt photos → `transactions` via pluggable OCR (`ocr_engines.py`) |
| `ingest_pdf.py` | PDF statements → `transactions` (text/OCR, local parser + opt-in `--ai`) |
| `pii.py` | Redact card/account/NPWP/phone/email/address from statement text |
| `categorize.py` | Merchant→category learning loop + backfill |
| `reconcile.py` | Report/remove fuzzy cross-source duplicate transactions |
| `generate_snapshot.py` | Monthly rollup → `snapshots` (income/expense/net worth/goals) |
| `export_analysis.py` | Snapshots → paste-ready, Indonesia-localized Claude prompt |
| `dropbox_sync.py` | Pull sheets/receipts/PDFs from a Dropbox folder into the pipeline |
| `dropbox_auth.py` | One-time helper to obtain a long-lived Dropbox refresh token |

## Deduplication
Every ingested row gets a content hash (`dedup_hash`), so re-importing a sheet
or an overlapping statement skips rows already present. PDF statements are also
hashed as whole files, so re-processing the same file is a no-op. `reconcile.py`
catches fuzzy near-duplicates (same amount, close dates, different source) that
an exact hash can't — e.g. a snapped receipt that also lands on a card statement.

## Privacy
Raw `transactions`, `family_profile`, `investments`, `assets`, and `debts` never
leave the machine. Two features can reach the network, both explicit and opt-in:
`export_analysis.py` (you paste the aggregate snapshot yourself) and
`ingest_pdf.py --ai` (sends only the **extracted statement text**, never the PDF,
to Claude to structure hard layouts). The default PDF parser is fully local. The
DB and all `data/` outputs are git-ignored.

### PII redaction (`pii.py`)
Before any `--ai` send, statement text is **redacted** — card numbers (incl.
Luhn-checked and 4-4-4-4 groups), bank account numbers, NPWP, phone, and email
are masked to `[CARD]`/`[ACCT]`/`[NPWP]`/`[PHONE]`/`[EMAIL]`; IDR amounts are
preserved. Card/account numbers are also stripped from transaction descriptions
before they're stored locally. Preview what would be removed:
```bash
python scripts/pii.py data/statements/bca.pdf --addresses
python scripts/ingest_pdf.py data/x.pdf --ai          # redaction on by default
python scripts/ingest_pdf.py data/x.pdf --ai --no-redact   # opt out (not advised)
```
Note: structured identifiers are removed deterministically; **cardholder names
and free-form addresses are only best-effort** (names need NER) — review the
`pii.py` preview if a statement puts a name in an unusual place.
