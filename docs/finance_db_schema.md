# Personal Finance Tracker — DB Schema & Sample Data

All amounts in IDR. Dates in ISO format (YYYY-MM-DD).

---

## 1. `accounts`
Labels only — not live-linked to any bank/e-wallet.

| id | name | type | starting_balance | currency |
|----|------|------|-------------------|----------|
| 1 | BCA Checking | bank | 15,000,000 | IDR |
| 2 | Cash | cash | 500,000 | IDR |
| 3 | GoPay | ewallet | 750,000 | IDR |
| 4 | OVO | ewallet | 300,000 | IDR |

---

## 2. `categories`
`type` = fixed / variable / irregular

| id | name | type | parent_category |
|----|------|------|------------------|
| 1 | Household | variable | NULL |
| 2 | Groceries | variable | Household |
| 3 | Utilities (PLN/PDAM/Internet) | fixed | Household |
| 4 | Cicilan (Loan/Mortgage) | fixed | NULL |
| 5 | School Fees | fixed | NULL |
| 6 | Transport | variable | NULL |
| 7 | Health/Medical | irregular | NULL |
| 8 | Entertainment | variable | NULL |
| 9 | Insurance Premium | fixed | NULL |
| 10 | Gifts/Donations | irregular | NULL |

---

## 3. `merchant_category_map`
Built up over time so the script auto-categorizes repeat merchants.

| id | merchant_name | category_id | confidence | last_confirmed |
|----|----------------|-------------|------------|------------------|
| 1 | Indomaret | 2 (Groceries) | confirmed | 2026-08-10 |
| 2 | PLN | 3 (Utilities) | confirmed | 2026-08-01 |
| 3 | Grab | 6 (Transport) | confirmed | 2026-08-15 |
| 4 | Sekolah Harapan Bangsa | 5 (School Fees) | confirmed | 2026-08-01 |
| 5 | Apotek K24 | 7 (Health) | confirmed | 2026-08-12 |

---

## 4. `transactions`
Raw ledger — stays local, never leaves your machine.

| id | date | amount | category_id | merchant | account_id | family_member | source | notes |
|----|------|--------|--------------|----------|------------|----------------|--------|-------|
| 1 | 2026-08-01 | 3,500,000 | 4 | Bank Mandiri | 1 | - | manual | Cicilan rumah |
| 2 | 2026-08-02 | 850,000 | 2 | Indomaret | 3 | Istri | receipt | Belanja mingguan |
| 3 | 2026-08-03 | 45,000 | 6 | Grab | 4 | - | manual | Transport kantor |
| 4 | 2026-08-05 | 2,000,000 | 5 | Sekolah Harapan Bangsa | 1 | Anak 1 | sheet | SPP Agustus |
| 5 | 2026-08-07 | 120,000 | 7 | Apotek K24 | 2 | Anak 2 | receipt | Obat batuk |
| 6 | 2026-08-10 | 350,000 | 8 | Cinema XXI | 3 | Keluarga | manual | Nonton bareng |

---

## 5. `family_profile`
Single-row (or small) reference table, not transactional.

| field | value |
|-------|-------|
| household_size | 4 |
| members | Saya (income), Istri (income), Anak 1 (8th grade), Anak 2 (5th grade) |
| monthly_income_total | 25,000,000 |
| income_sources | Gaji Saya: 15,000,000 (monthly); Gaji Istri: 8,000,000 (monthly); Freelance: 2,000,000 (variable) |
| fixed_obligations_total | 6,850,000 |

---

## 6. `budgets`
Monthly limits per category, used for the "sidetracked" alerts.

| category_id | month | monthly_limit |
|-------------|-------|-----------------|
| 2 (Groceries) | 2026-08 | 3,500,000 |
| 6 (Transport) | 2026-08 | 1,000,000 |
| 8 (Entertainment) | 2026-08 | 800,000 |

---

## 7. `goals`

| id | name | target_amount | current_amount* | target_date | priority |
|----|------|----------------|-------------------|--------------|----------|
| 1 | Emergency Fund | 75,000,000 | (derived) | 2027-06-01 | High |
| 2 | House Down Payment | 200,000,000 | (derived) | 2029-01-01 | High |
| 3 | Anak 1 - Dana Kuliah | 150,000,000 | (derived) | 2032-07-01 | Medium |

*`current_amount` is not stored directly — it's computed as `SUM(goal_contributions.amount)` for that goal_id.

---

## 8. `goal_contributions`

| id | goal_id | date | amount | source | notes |
|----|---------|------|--------|--------|-------|
| 1 | 1 | 2026-07-01 | 2,000,000 | monthly allocation | Auto transfer |
| 2 | 1 | 2026-08-01 | 2,000,000 | monthly allocation | Auto transfer |
| 3 | 2 | 2026-07-15 | 3,000,000 | monthly allocation | Manual transfer |
| 4 | 2 | 2026-08-15 | 1,500,000 | leftover after budget | Sisa bulan ini |
| 5 | 3 | 2026-08-01 | 500,000 | monthly allocation | Reksadana pendidikan |

---

## 9. `snapshots`
Monthly rollup — computed, not manually entered. **This is what gets sent to Claude for analysis** (raw transactions/family_profile never leave your machine).

| month | total_income | total_expense | savings_rate | groceries_actual | groceries_budget | goal_1_progress | goal_2_progress |
|-------|----------------|------------------|----------------|---------------------|----------------------|-------------------|-------------------|
| 2026-07 | 25,000,000 | 18,200,000 | 27.2% | 3,300,000 | 3,500,000 | 22,000,000 / 75,000,000 | 45,000,000 / 200,000,000 |
| 2026-08 | 25,000,000 | 19,865,000 | 20.5% | 850,000 (partial mo.) | 3,500,000 | 24,000,000 / 75,000,000 | 49,500,000 / 200,000,000 |

---

## What actually gets sent to Claude for analysis
Only the `snapshots` table (+ `goals` metadata for target/date context) — no merchant names, no per-transaction detail, no family member identities needed unless you want per-member breakdowns in the advice.

Want me to write the Python script that builds this SQLite DB and populates it from a CSV + a folder of receipt photos?
