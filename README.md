# Personal Finance Advisor

A privacy-first, Indonesia-localized personal finance tracker and AI-assisted advisor.

- Raw financial data stays local (SQLite)
- Only aggregated monthly snapshots are shared with Claude for analysis
- Ingests expense sheets and receipt photos (no direct bank account linking)

See `docs/PLANNING.md` for architecture and `docs/finance_db_schema.md` for the full schema with sample data.

## Status
In progress — schema finalized. Build step 1 done: `scripts/init_db.py` creates the SQLite DB.

## Setup
```bash
python scripts/init_db.py --reset --seed   # fresh DB (data/finance.db) with sample data
python scripts/init_db.py                   # create schema only, if missing
```
