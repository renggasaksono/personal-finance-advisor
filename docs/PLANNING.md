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
[Python ingestion script]
  - OCR (pytesseract / easyocr) on receipt images (in-memory, not stored)
  - pandas parsing for CSV/XLSX sheets
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
- Google Drive folder (receipts, sheets) — via `google-api-python-client`
- Dropbox folder — via `dropbox` SDK
- Manual entry (CLI or simple form) for anything not photographed/exported

Images/receipts are **not retained** after parsing — processed in-memory or temp file, then discarded. Source of truth for the original stays in Drive/Dropbox if ever needed for manual re-verification.

---

## Database Schema (SQLite)

### Core cash flow
- `accounts` — bank/cash/e-wallet labels (not live-linked)
- `categories` — fixed / variable / irregular, with parent/child structure
- `merchant_category_map` — learned merchant -> category mapping, built up over time
- `transactions` — raw ledger (local only, never transmitted)
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

### Reporting
- `snapshots` — monthly rollup (income, expense, savings rate, category totals, goal progress). **This is the only table whose contents get sent to Claude for analysis.**

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
- [ ] Auto-run (cron, checks Drive/Dropbox folder daily) vs. on-demand (manual trigger after uploading receipts)
- [ ] OCR engine choice: `pytesseract` (simpler, needs Tesseract installed) vs. `easyocr` (better accuracy, heavier dependency) vs. Claude vision for ambiguous receipts
- [ ] Where snapshot -> Claude analysis happens: paste into chat manually, or automate via Anthropic API call from the script
- [ ] Asset/investment valuation updates: manual entry cadence (monthly? quarterly?) vs. scraping public price sources

---

## Build Order
1. SQLite schema + migration script (`scripts/init_db.py`)
2. Sheet ingestion (`scripts/ingest_sheet.py`) — pandas-based, simplest path first
3. Receipt OCR ingestion (`scripts/ingest_receipt.py`) — local files first, then Drive/Dropbox integration
4. Merchant categorization logic + `merchant_category_map` learning loop
5. Snapshot generation job (`scripts/generate_snapshot.py`)
6. Claude analysis integration (manual paste first, API automation later if desired)

---

## Privacy Notes
- Raw `transactions`, `family_profile`, `investments`, `assets`, `debts` never leave the local machine
- Only `snapshots` (+ `goals` metadata) are shared externally, and only when analysis is requested
- No images retained after OCR parsing
- `.gitignore` excludes the actual SQLite DB file and any real personal data — only schema/scripts are versioned
