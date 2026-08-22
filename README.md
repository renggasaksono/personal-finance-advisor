# Personal Finance Advisor

A privacy-first, Indonesia-localized personal finance tracker and AI-assisted advisor.

- Raw financial data stays local (SQLite)
- Only aggregated monthly snapshots are shared with Claude for analysis
- Ingests expense sheets and receipt photos (no direct bank account linking)

See `docs/PLANNING.md` for architecture and `docs/finance_db_schema.md` for the full schema with sample data.

## Status
Pipeline complete (build steps 1–6). All ingestion, categorization, snapshot,
and export scripts are in `scripts/`.

## Setup
```bash
pip install -r requirements.txt             # pandas/openpyxl/pillow/pytesseract
python scripts/init_db.py --reset --seed    # fresh DB (data/finance.db) with sample data
```
Receipt OCR also needs the Tesseract binary (`brew install tesseract tesseract-lang`
on macOS). SQLite scripts (init/categorize/snapshot/export) use only the stdlib.

## Workflow
```bash
# 1. Create / reset the local DB (source of truth, git-ignored)
python scripts/init_db.py --reset --seed

# 2. Ingest expense data
python scripts/ingest_sheet.py   data/august.csv --account "BCA Checking"
python scripts/ingest_receipt.py data/receipts/ --dry-run   # preview OCR first
python scripts/ingest_receipt.py data/receipts/

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

## Scripts
| Script | Purpose |
|--------|---------|
| `init_db.py` | Create the SQLite schema (14 tables); `--seed` loads sample data |
| `ingest_sheet.py` | CSV/XLSX → `transactions` (pandas, IDR/date parsing, auto-categorize) |
| `ingest_receipt.py` | Receipt photos → `transactions` via pluggable OCR (`ocr_engines.py`) |
| `categorize.py` | Merchant→category learning loop + backfill |
| `generate_snapshot.py` | Monthly rollup → `snapshots` (income/expense/net worth/goals) |
| `export_analysis.py` | Snapshots → paste-ready, Indonesia-localized Claude prompt |

## Privacy
Raw `transactions`, `family_profile`, `investments`, `assets`, and `debts` never
leave the machine. Only `snapshots` (aggregates + goal targets) are exported, and
only when you run `export_analysis.py` and paste the result yourself — no API keys,
no automatic egress. The DB and all `data/` outputs are git-ignored.
