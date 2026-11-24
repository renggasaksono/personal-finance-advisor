# Personal Finance Advisor — Project Plan

## Goal
A privacy-first personal finance system, localized for Indonesia, that:
- Ingests expense data from sheets and receipt photos (no direct bank/account linking)
- Tracks financial goals, family expenses, investments, assets, and debts
- Uses AI (Claude) only for the interpretive layer — analysis, recommendations, and market-context reasoning
- Keeps raw data local; only aggregated summaries are ever sent externally

---

## Architecture

```
[Google Drive / Dropbox folder]
        |  (receipt photos, expense sheets)
        v
[Python ingestion script]  (one entry point: scripts/ingest.py, routes by type)
  - OCR (pytesseract, pluggable) on receipt images (in-memory, not stored)
  - pandas parsing for CSV/XLSX sheets
  - PDF statements (CC / e-wallet / bank): pdfplumber text, OCR fallback for
    scans; local heuristic parser by default, opt-in Claude structuring (--ai,
    sends extracted text only) for hard layouts
  - Deduplication (content hash per row + file hash per statement)
  - Merchant -> category matching (merchant_category_map)
        v
[SQLite local DB]  <-- source of truth, never leaves the machine
        |
        v
[Monthly snapshot job]
  - Aggregates transactions into snapshots table
  - No raw transactions, no images, no PII beyond what's needed
        v
[Claude API / chat]
  - Receives only snapshots + goals + investment/net-worth summary
  - Performs analysis, trend detection, localized (Indonesia) market reasoning
  - Returns recommendations, alerts ("sidetracked" detection)
```

**Design principle:** Python does 100% of the deterministic work (parsing, OCR text extraction,
categorization via lookup table, arithmetic, rollups). AI is used only for:
1. Interpreting messy/novel OCR text when merchant is unrecognized
2. Final-stage analysis and recommendations
3. Localized market/economic reasoning (inflation, BI rate, investment climate) — requires live web search, not static knowledge

---

## Data Sources
- Local files (CSV/XLSX sheets, receipt images, PDF statements) via `scripts/ingest.py`
- Dropbox folder — via `dropbox` SDK (`scripts/dropbox_sync.py`, refresh-token auth) — **implemented**
- Google Drive folder — planned, not yet built
- Manual entry (CLI or simple form) for anything not photographed/exported

Images/receipts/PDFs are **not retained** after parsing — processed in-memory or a temp file that is deleted, then discarded (Dropbox pulls download to a temp dir that is cleaned up). Source of truth for the original stays in Dropbox if ever needed for manual re-verification.

---

## Database Schema (SQLite)

### Core cash flow
- `accounts` — bank/cash/e-wallet/credit labels (not live-linked); `credit` = liability (card, paylater)
- `categories` — fixed / variable / irregular, with parent/child structure
- `merchant_category_map` — learned merchant -> category mapping, built up over time
- `transactions` — raw ledger (local only, never transmitted); carries `dedup_hash` + `external_ref` for deduplication and `source` ∈ manual/sheet/receipt/statement; `type` ∈ expense/income/transfer (transfer = own-account move: card bill payment, e-wallet top-up)
- `budgets` — target % of income per category per month (not fixed numbers, since income varies)

### Goals & family
- `family_profile` — household composition, income sources, fixed obligations total
- `goals` — target amount, target date, priority
- `goal_contributions` — dated contributions per goal (current_amount is derived, not stored)

### Net worth
- `investments` — holdings (reksadana, SBN, saham, emas, etc.)
- `investment_transactions` — buy/sell/dividend/coupon history
- `investment_valuations` — periodic price snapshots (manual or scraped)
- `assets` — non-investment assets (property, vehicle, valuables) with acquired vs. current est. value
- `debts` — liabilities with current_balance, interest_rate, monthly_payment, linked_asset_id

### Reporting & provenance
- `snapshots` — monthly rollup (income + `income_basis` actual/baseline, expense, savings rate, category totals, goal progress). **This is the only table whose contents get sent to Claude for analysis** (via `export_analysis.py`).
- `statements` — one row per ingested PDF/statement file, tracked by `file_hash` so re-importing the same file is a no-op (period, source_type, row_count).

Net worth formula:
```
net_worth = SUM(accounts.balance)
          + SUM(investment_valuations.market_value)
          + SUM(assets.current_est_value)
          - SUM(debts.current_balance)
```

Debt-to-income check:
```
dti_ratio = SUM(debts.monthly_payment) / monthly_income
```

Full schema with sample data lives in `docs/finance_db_schema.md`.

---

## Open Decisions
- [x] OCR engine — **pluggable, `pytesseract` default** (`scripts/ocr_engines.py`); easyocr selectable, Claude vision stays opt-in only for privacy
- [x] Snapshot -> Claude handoff — **export-only** (`export_analysis.py` writes a paste-ready prompt; no auto API egress)
- [x] PDF statement parsing — **local heuristic parser by default, opt-in `--ai`** (sends extracted text only) for hard layouts
- [x] Duplicate handling — **content-hash dedup at ingest + `reconcile.py`** for fuzzy cross-source near-duplicates
- [ ] Auto-run (cron, checks Dropbox folder daily) vs. on-demand (manual trigger)
- [ ] Google Drive integration (Dropbox is done)
- [ ] Asset/investment valuation updates: manual entry cadence vs. scraping public price sources

---

## Build Order
1. ✅ SQLite schema + init (`scripts/init_db.py`); migrations in `scripts/migrate.py`
2. ✅ Sheet ingestion (`scripts/ingest_sheet.py`) — pandas-based
3. ✅ Receipt OCR ingestion (`scripts/ingest_receipt.py`) — pluggable engines
4. ✅ Merchant categorization + `merchant_category_map` learning loop (`scripts/categorize.py`)
5. ✅ Snapshot generation (`scripts/generate_snapshot.py`)
6. ✅ Claude analysis export (`scripts/export_analysis.py`) — paste-ready prompt
7. ✅ Dropbox sync (`scripts/dropbox_sync.py`, `dropbox_auth.py`)
8. ✅ PDF statement ingestion + dedup + reconcile (`scripts/ingest_pdf.py`,
   `ingest_core.py`, `reconcile.py`) and a unified entry point (`scripts/ingest.py`)

---

## Privacy Notes
- Raw `transactions`, `family_profile`, `investments`, `assets`, `debts` never leave the local machine
- Only `snapshots` (+ `goals` metadata) are shared externally, and only when analysis is requested
- No images or PDFs retained after parsing (temp files are deleted; Dropbox originals stay in the user's account)
- One deliberate, opt-in exception to "raw stays local": `ingest_pdf.py --ai` sends the **extracted statement text** (never the file) to Claude to structure hard layouts. The default PDF parser is fully local.
- PII redaction (`scripts/pii.py`) runs before any `--ai` send — card/account numbers, NPWP, phone, and email are masked (IDR amounts preserved); card/account numbers are also stripped from stored descriptions. Names/addresses are best-effort only.
- `.gitignore` excludes the actual SQLite DB file and any real personal data (incl. `data/*.md` exports) — only schema/scripts are versioned
