# Derivatives Risk Platform — Build Plan

> **Integrated Market Risk (VaR) + Counterparty Credit Exposure (PFE) for Capital Markets Derivatives**
>
> End-to-end lakehouse prototype deployed via Declarative Automation Bundles (DABs), covering Lakeflow ingestion, Unity Catalog governance, Lakebase operational serving, ML/GenAI intelligence, a Genie agent for natural-language queries, and a Databricks App for business users.

---

## 0. Project Parameters

| Parameter | Default | Notes |
| --- | --- | --- |
| `catalog` | `serverless_stable_7s0j2u_catalog` | User's main catalog |
| `schema` | `market` | All risk tables live here |
| `n_paths` | `1000` | Monte Carlo paths |
| `lakebase_instance` | (provisioned at setup) | Postgres-compatible serving layer |
| `fred_secret` | `serverless_stable_7s0j2u_catalog.brian_mcconnell.fred_api_key` | UC secret for FRED API |
| `backfill_start_date` | today minus 10 years | Earliest history to load; 10Y needed for the 10Y vol tenor, minimum 2Y for correlation |
| `backfill_end_date` | yesterday | Last date loaded by backfill; daily incremental takes over from the next business day |
| `backfill_chunk_months` | `12` | Size of each date chunk, so a failed chunk can be rerun on its own |

---

## 1. Repo & Bundle Layout

```
derivatives-risk-platform/
├── databricks.yml                  # DABs bundle config (dev + prod targets)
├── resources/
│   ├── jobs/
│   │   ├── setup_job.yml           # One-time schema/table/Lakebase creation
│   │   ├── backfill_job.yml        # Historic market rates backfill (default 10Y, min 2Y)
│   │   └── risk_daily_job.yml      # Main daily orchestration
│   ├── pipelines/
│   │   ├── market_data_pipeline.yml
│   │   └── trade_pipeline.yml
│   ├── dashboards/
│   │   ├── market_sim_dashboard.yml
│   │   └── risk_dashboard.yml
│   ├── lakebase/
│   │   └── synced_tables.yml
│   └── genie/
│       └── risk_analyst_space.yml
├── src/
│   ├── riskcore/                   # Python wheel
│   │   ├── __init__.py
│   │   ├── curves.py               # QuantLib yield curve bootstrap
│   │   ├── vol.py                  # Historical volatility (log-return, EWMA)
│   │   ├── corr.py                 # Correlation matrix (2Y window, SVD)
│   │   ├── sim.py                  # Monte Carlo path generation
│   │   ├── pricers_ql.py           # QuantLib pricers (accurate, single-trade)
│   │   ├── pricers_vec.py          # Vectorised NumPy pricers (batch grid)
│   │   ├── exposure.py             # Netting, PFE aggregation
│   │   ├── var.py                  # Book-level VaR
│   │   └── tradegen.py             # Synthetic trade generator
│   ├── pipelines/
│   │   ├── market_bronze.py        # SDP: raw → bronze market data
│   │   ├── market_silver.py        # SDP: bronze → silver (typed, deduped)
│   │   ├── trade_bronze.py         # SDP: Auto Loader raw files → bronze
│   │   └── trade_silver.py         # SDP: bronze → per-product silver + quarantine
│   ├── notebooks/
│   │   ├── 00_setup.py
│   │   ├── 01_ingest_fred.py
│   │   ├── 01_ingest_yfinance.py
│   │   ├── 01b_backfill_market.py      # Historic market rates backfill (chunked, idempotent)
│   │   ├── 01c_validate_backfill.py    # Coverage, gap and spot-value checks
│   │   ├── 02b_backfill_analytics.py   # Historic curves, vol, correlation, HW calibration
│   │   ├── 02_bootstrap_curves.py
│   │   ├── 03_calc_hist_vol.py
│   │   ├── 04_calc_correlation.py
│   │   ├── 05_mc_simulation.py
│   │   ├── 06_bloomberg_adapter.py
│   │   ├── 08_generate_trades.py
│   │   ├── 10_dqx_monitors.py
│   │   ├── 13_calc_exposure.py
│   │   ├── 14_calc_var.py
│   │   └── 99_collect_evidence.py
│   └── app/
│       ├── app.py                  # Databricks App (Dash/Gradio)
│       └── app.yaml
├── tests/
│   ├── test_pricers.py             # QuantLib vs vectorised cross-check
│   ├── test_tradegen.py
│   └── test_exposure.py
├── docs/
│   └── methodology.md              # Neutral methodology write-up (no confidential text)
├── evidence/                       # Execution evidence (committed with outputs)
│   ├── notebooks/                  # .ipynb with cell outputs from real runs
│   ├── runs/                       # Job run logs as markdown
│   ├── results/                    # Query result snapshots
│   └── tests.log                   # pytest output
├── scripts/
│   └── collect_evidence.py         # SDK script to harvest run logs
└── PLAN.md                         # This file
```

---

## 2. Existing Workspace Assets to Reuse

These are already working in this workspace and will be adapted into the new bundle:

| Asset | Location | What it provides |
| --- | --- | --- |
| FRED rates loader | `.bundle/quant-dev-platform/.../notebooks/data/fred_rates_loader` | 44 FRED series, backfill + incremental modes, UC secret pattern |
| yfinance equity loader | `demos/Projects/Hedgies/Databricks_equity_analytics` | `yf.download` pattern with incremental updates |
| QuantLib curve bootstrap | `.bundle/yield_curve_demo/.../src/yield_curve_bootstrap.py` | `TreasuryYieldCurveBootstrap` class, FRED DGS\* series |
| Hull-White 2F simulation | `.bundle/yield_curve_demo/.../src/hull_white_2factor_simulation.py` | Two-factor Hull-White model |
| FRED API key | UC secret: `serverless_stable_7s0j2u_catalog.brian_mcconnell.fred_api_key` | Authentication |

---

## 3. Lakehouse Schema Design (Medallion Architecture)

All tables in `{catalog}.market`.

### Bronze (raw ingested)

| Table | Source | Format |
| --- | --- | --- |
| `bronze_fred_rates` | FRED API (44 series) | Append-only, daily |
| `bronze_equity_prices` | yfinance | Append-only, daily |
| `bronze_fx_rates` | FRED + ECB/BoE public | Append-only, daily |
| `bronze_bond_prices` | Bloomberg adapter (synthetic default) | Append-only, daily |
| `bronze_bond_ref` | Bloomberg adapter | SCD Type 1 |
| `bronze_credit_spreads` | FRED ICE BofA OAS + synthetic CDS | Append-only, daily |
| `bronze_trades_irs` | Auto Loader (parquet) | Append-only |
| `bronze_trades_xccy` | Auto Loader (csv) | Append-only |
| `bronze_trades_fra` | Auto Loader (csv) | Append-only |
| `bronze_trades_fxfwd` | Auto Loader (parquet) | Append-only |
| `bronze_trades_euro_opt` | Auto Loader (xml) | Append-only |
| `bronze_trades_amer_opt` | Auto Loader (xml) | Append-only |

### Silver (cleansed, typed, validated)

| Table | Key transforms |
| --- | --- |
| `silver_rates` | Date parsing, NULL handling, units normalised to decimal, business-day aligned |
| `silver_fx` | Cross-rate derivation (all vs USD base), deduplication |
| `silver_equity` | Adjusted close, corporate-action handling, gap-filling |
| `silver_bonds` | Price validation, yield calculation |
| `silver_bond_ref` | Issuer, maturity, coupon, currency enrichment |
| `silver_credit_spreads` | Rating-mapped, interpolated term structure |
| `silver_trades_irs` | Schema enforcement, QuantLib-ready fields |
| `silver_trades_xccy` | Schema enforcement, QuantLib-ready fields |
| `silver_trades_fra` | Schema enforcement, QuantLib-ready fields |
| `silver_trades_fxfwd` | Schema enforcement, QuantLib-ready fields |
| `silver_trades_euro_opt` | Schema enforcement, QuantLib-ready fields |
| `silver_trades_amer_opt` | Schema enforcement, QuantLib-ready fields |
| `quarantine_trades` | Failed expectations: trade_id, product, rule_name, reason, raw_payload |

### Gold (analytics-ready)

| Table | Description |
| --- | --- |
| `gold_zero_curves` | date, ccy, tenor, zero_rate, discount_factor — full history |
| `gold_hist_vol` | date, risk_factor, tenor, vol_1d, vol_ewma, vol_3m, vol_1y, vol_3y, vol_10y |
| `gold_corr_matrix` | calc_date, factor_i, factor_j, correlation — weekly snapshots |
| `gold_sim_curves` | sim_date, ccy, tenor, path_id, zero_rate, discount_factor |
| `gold_sim_fx` | sim_date, ccy_pair, path_id, fx_rate |
| `gold_sim_equity` | sim_date, ticker, path_id, price |
| `gold_cpty_exposure` | counterparty_id, netting_set_id, time_step, ee, pfe_95 |
| `gold_book_var` | book_id, time_step, mean_pv, p5_pv, p95_pv, var_95 |
| `gold_trade_prices_sample` | trade_id, time_step, path_id, pv — for 50 drill-down paths |

### Dimension Tables

| Table | Key fields |
| --- | --- |
| `dim_book` | book_id, desk, business_unit |
| `dim_counterparty` | counterparty_id, name, rating, sector, country |
| `dim_netting_agreement` | netting_set_id, counterparty_id, agreement_type (ISDA/NA) |
| `dim_collateral_agreement` | collateral_id, netting_set_id, threshold, mta, mpor, agreement_status (CSA/NA) |

### Data Quality

| Table | Description |
| --- | --- |
| `dq_results` | DQX check results per table per run |
| `dq_alerts` | Flagged issues: rate moves > 1σ, notionals > $10M, large DoD changes |

### Control & Calibration

| Table | Description |
| --- | --- |
| `ctl_load_watermark` | source, series_id, first_obs_date, last_obs_date, last_load_ts, load_mode (backfill/incremental) |
| `ctl_backfill_log` | run_id, source, chunk_start, chunk_end, rows_loaded, status, error_message |
| `gold_hw_params` | calib_date, ccy, alpha, sigma, calibration_case (1–7), is_fallback — weekly Hull-White calibration history |

---

## 4. Build Stages — Detailed

### Stage 0: Setup Job

**What it does:**
- Creates `{catalog}.market` schema
- Creates UC volumes: `raw_market`, `raw_trades`, `checkpoints`
- Creates all tables with full column comments and UC tags
- Provisions the Lakebase instance and database
- Sets up grants for the service principal

**Implementation:** Single notebook `00_setup.py` with idempotent `CREATE IF NOT EXISTS` statements.

---

### Stage 1: Market Data Ingestion (Daily)

**FRED Rates** (`01_ingest_fred.py`)
- Reuses the existing `fred_rates_loader` pattern (44 series, backfill/incremental)
- Series: Treasury yields (1M–30Y), SOFR, Fed Funds, TIPS, inflation breakevens, credit OAS (AAA–HY), FX spot rates (EUR, GBP, JPY, CAD, CNY, CHF)
- Writes to `bronze_fred_rates`

**Equity Prices** (`01_ingest_yfinance.py`)
- Reuses `yf.download` pattern from the equity analytics notebook
- Universe: ~50 liquid equities (S&P 500 constituents sample) + major indices (SPX, FTSE, N225, DAX, HSI)
- Writes to `bronze_equity_prices`

**FX Rates**
- USD-base pairs from FRED (DEXUSEU, DEXJPUS, DEXUSUK, etc.)
- EUR and GBP supplemented from ECB Statistical Data Warehouse and BoE public APIs
- Writes to `bronze_fx_rates`

**SDP Pipeline: `market_data_pipeline`**
- Bronze → Silver with expectations:
  - `expect("rate_not_null", "value IS NOT NULL")`
  - `expect("date_valid", "obs_date <= current_date()")`
  - `expect("rate_in_range", "value BETWEEN -5 AND 100")`
- Deduplication on (series_id, obs_date)
- Business-day alignment using a calendar table

---

### Stage 1b: Historic Market Rates Backfill (One-Time, Rerunnable)

**Notebooks:** `01b_backfill_market.py`, `01c_validate_backfill.py`, `02b_backfill_analytics.py`

**Purpose:** Load enough history to calibrate the models before the first daily run. The 10Y vol tenor and the 10-year discount-factor vol ratio used for Hull-White need about 10 years; correlation needs 2 years. The default backfill window is 10 years, with 2 years as the hard minimum.

**Step 1 — Load raw history by source** (all written as raw files to the `raw_market` volume, partitioned by `source/series_id/year`):

| Source | Series | Method |
| --- | --- | --- |
| FRED | All 44 registry series (Treasury CMT 1M–30Y, SOFR, Fed Funds, TIPS, breakevens, OAS, FX DEX\*) | Existing `fred_rates_loader` in `backfill` mode with `observation_start = backfill_start_date`; one request per series, throttled below the FRED limit (120 requests/min) with retry and backoff |
| FRED | €STR (`ECBESTRVOLWGTTRMDMNRT`), SONIA (`IUDSOIA`) | Same loader, added to the series registry |
| yfinance | ~50 equities + indices (SPX, FTSE, N225, DAX, HSI, MSCI country proxies) | `yf.download` in batches of ~10 tickers and `backfill_chunk_months` date chunks, `auto_adjust=True`; pause between batches to avoid rate limiting |
| yfinance | FX crosses (`EURUSD=X`, `GBPUSD=X`, …) | Used only to fill gaps where FRED DEX\* series are missing |
| ECB Data Portal | EUR AAA government yield curve (YC dataset), EXR reference FX rates | REST API (SDMX/CSV), chunked by year |
| Bank of England | Gilt nominal spot curve archive, SONIA | Published archive files, parsed into tenor/rate rows |
| OECD | JPY, CHF, CAD long-term and 3M rates | Monthly data, forward-filled to business days and flagged `is_proxy = true` |

**Step 2 — Idempotent load to bronze/silver:**
- Run `market_data_pipeline` with a full refresh so the SDP expectations apply to history exactly as they do to daily data
- Silver keyed on (source, series_id, obs_date); reruns overwrite rather than duplicate
- Each chunk is recorded in `ctl_backfill_log`; a failed chunk is retried on its own without reloading the rest

**Step 3 — Validate coverage** (`01c_validate_backfill.py`):
- Per series: first/last date, row count, and missing business days compared with the relevant holiday calendar (US, TARGET, UK, Japan, Switzerland, Canada)
- Gaps up to 5 business days are forward-filled and flagged; longer gaps fail the check
- Spot checks against a few known published values (e.g., DGS10 on fixed reference dates)
- Results written to `dq_results` and exported to `evidence/results/backfill_coverage.md`

**Step 4 — Rebuild historic analytics** (`02b_backfill_analytics.py`):
- **Zero curves:** bootstrap every business day in the window for every currency (~2,600 days × 6 ccy). Run in parallel by date with `applyInPandas`, writing to `gold_zero_curves` by date range (`replaceWhere`) so reruns are safe
- **Historic vol:** rolling and EWMA vols over the full history into `gold_hist_vol`
- **Correlation:** one snapshot for every Monday from `backfill_start_date + 2Y` onward into `gold_corr_matrix`
- **Hull-White calibration:** weekly alpha/sigma per currency with the Cases 1–7 fallback rules into `gold_hw_params`

**Step 5 — Hand over to daily incremental:**
- Write `ctl_load_watermark` with the last loaded date per series
- The daily `ingest_*` tasks read the watermark and load only from `last_obs_date + 1`, so there is no gap or overlap between backfill and daily loads
- The daily job refuses to start the simulation if the watermark shows less than 2 years of history

---

### Stage 2: Yield Curve Bootstrap

**Notebook:** `02_bootstrap_curves.py`

**Method:**
- QuantLib `PiecewiseLogCubicDiscount` per currency per day
- USD: Treasury CMT rates (11 tenors: 1M to 30Y)
- EUR: €STR + ECB AAA curve tenors
- GBP: SONIA + BoE gilt curve
- JPY, CHF, CAD: OECD government bond yields + overnight rate proxies

**Output:** Appends to `gold_zero_curves` (date, ccy, tenor, zero_rate, discount_factor)

**History:** Full history is kept — never overwritten. Each day's bootstrap is a new partition.

---

### Stage 3: Historical Volatility

**Notebook:** `03_calc_hist_vol.py`

**Method:**
- Log returns on zero rates (using 1Y discount factor as short-rate proxy), FX spot, equity prices
- EWMA volatility (lambda = 0.94, RiskMetrics convention)
- Term structure at 3M, 1Y, 3Y, 10Y tenors (annualised)
- FX volatility floor: 3% (per methodology)

**Output:** `gold_hist_vol`

---

### Stage 4: Correlation Matrix (Weekly)

**Notebook:** `04_calc_correlation.py`

**Method (per methodology):**
- 2 years of daily log returns
- FX and equity: log returns of spot
- Interest rates: 1Y discount factor as short-rate proxy; correlation sign flipped (ρ(d, γ) = -1 · ρ(b, γ))
- Nearest positive-semi-definite correction
- SVD decomposition for simulation factors
- Standard Pearson correlation: ρ_xy = (1/n) Σ(x_i - μ_x)(y_i - μ_y) / (σ_x · σ_y)

**Output:** `gold_corr_matrix` (long format: calc_date, factor_i, factor_j, correlation)

**Schedule:** Runs on Mondays only (weekly, matching methodology's weekly calibration).

---

### Stage 5: Monte Carlo Simulation

**Notebook:** `05_mc_simulation.py`

**Simulation Time Grid (per requirements):**

| Period | Granularity | Approx. steps |
| --- | --- | --- |
| Days 1–10 | Daily | 10 |
| Weeks 2–5 | Weekly | 4 |
| Months 2–24 | Monthly | 22 |
| Years 3–10 | Annual | 8 |
| **Total** | | **~44 steps** |

**Models (per methodology):**

*Interest Rates — Extended Vasicek (Hull-White 1-Factor):*
- dr(t) = α(θ(t) - r(t))dt + σ dW
- θ(t) calibrated to match initial discount factors
- α calibrated from historical discount-factor volatility ratio: M¹/M⁰ = (1 - e^(-α)) / (1 - e^(-10α))
- Bounds: 0.1 < α < 1, 0 < σ < 0.022
- Fallback: α = 0.1 (floor), σ = 0.022 (ceiling)
- Full algorithm with Cases 1–7 as per methodology
- Simulated discount factors from short rate + initial curve

*FX — Geometric Brownian Motion:*
- dF/F = (m_f + r_d - r_f)dt + σ_FX dZ
- Risk premium m_f = 0
- Driven by domestic and foreign simulated short rates
- Instantaneous vol from forward vol term structure with 3% floor
- Interpolation: log-linear between simulation dates

*Equities — Lognormal:*
- S(t+1) = S(t) · exp{(r - q - ρσ_FX - ½σ²)Δt + σ√Δt · Z_EQ}
- Z_EQ = S_Eq,CI · Z_CI + √(1 - S²_Eq,CI) · Z_ID (country index decomposition)
- S_Eq,CI = correlation between equity and MSCI country index
- Instantaneous vol derived from vol term structure

*Random Numbers:*
- Seed: weekly integer date code (days since 30 Dec 1899)
- Correlated via SVD-factored correlation matrix
- Box-Muller transform for normal variates

**Output:** `gold_sim_curves`, `gold_sim_fx`, `gold_sim_equity`

**Scale:** ~1000 paths × ~44 time steps × ~30 risk factors = ~1.3M scenario points per run.

---

### Stage 6: Bloomberg Adapter

**Notebook:** `06_bloomberg_adapter.py`

**Design:**
- Abstract adapter interface with two implementations:
  - `SyntheticBloombergAdapter` (default) — generates realistic bond prices, reference data, and credit spreads synthetically
  - `BloombergAPIAdapter` — wraps BLPAPI for real Bloomberg terminals (not used in prototype; data cannot be published)
- Credit spreads: FRED ICE BofA OAS series (public) mapped to rating buckets, supplemented with synthetic per-issuer spreads
- Bond universe: ~500 synthetic government + corporate bonds across USD, EUR, GBP, JPY

**Output:** `bronze_bond_prices`, `bronze_bond_ref`, `bronze_credit_spreads`

---

### Stage 7: Dashboards

**AI/BI Dashboard: `market_sim_dashboard`**
- Fan charts of simulated yield curves by time step (percentile bands)
- FX rate simulation paths with confidence intervals
- Equity price simulation distributions
- Correlation heatmap (latest weekly snapshot)
- Volatility history by risk factor
- Rate change alerts from DQX

**AI/BI Dashboard: `risk_dashboard`**
- PFE profiles by counterparty (time-step line charts with 95th percentile)
- VaR by book (bar chart with mean, P5, P95)
- Top 10 counterparty exposures
- Trade quarantine summary
- DQX alert feed

---

### Stage 8: Synthetic Trade Generator (Daily)

**Notebook:** `08_generate_trades.py`

**Trade Types and Counts:**

| Product | Count | Format | Key Fields |
| --- | --- | --- | --- |
| Interest Rate Swaps | 50,000+ | Parquet | trade_id, book_id, cpty_id, netting_set_id, collateral_id, ccy, notional, fixed_rate, float_index, pay_freq, maturity, start_date, direction |
| Cross-Currency Swaps | 50,000+ | CSV | As IRS + ccy_2, notional_2, fx_rate_at_inception |
| FRAs | 50,000+ | CSV | trade_id, book_id, cpty_id, netting_set_id, collateral_id, ccy, notional, fixed_rate, start_date, end_date, settlement_date |
| FX Forwards | 50,000+ | Parquet | trade_id, book_id, cpty_id, netting_set_id, collateral_id, ccy_pair, notional, forward_rate, maturity |
| European Equity Options | 50,000+ | XML | trade_id, book_id, cpty_id, netting_set_id, collateral_id, underlying, strike, maturity, option_type (call/put), notional |
| American Equity Options | 50,000+ | XML | As European + exercise_style=American |

**Data Quality Injection:**
- At least 500 trades across types with intentionally missing data (NULL notional, missing maturity, invalid currency, missing counterparty)
- Distributed randomly across products

**Book/Counterparty Distribution:**
- ~25 books, ~300 counterparties
- Zipf distribution: at least 10 books and at least 1 counterparty have a disproportionately large trade set
- Each trade assigned exactly one book_id and one counterparty_id

**Netting & Collateral Agreements:**
- Netting agreement IDs unique per counterparty (not shared across counterparties)
- At least 1/3 of netting agreements = "NA" (no agreement)
- At least 1/3 of collateral agreements = "NA" (no agreement)
- Collateral agreements have threshold, MTA, MPoR fields

**Day-on-Day Behaviour:**
- Some trades amended (notional changes, maturity extensions)
- New trades added daily
- Trade files written to `raw_trades` volume, partitioned by product and date

---

### Stage 9: Trade SDP Pipeline (Auto Loader + Quarantine)

**Pipeline:** `trade_pipeline`

**Auto Loader Configuration:**
- Per-format cloud files sources watching `raw_trades/{product}/` directories
- Parquet: schema evolution enabled
- CSV: header inference, schema hints
- XML: `com.databricks.spark.xml` with row tag per product
- Checkpoint location: `checkpoints` volume

**Bronze → Silver Expectations:**

```
@expect_or_drop("valid_notional", "notional IS NOT NULL AND notional > 0")
@expect_or_drop("valid_maturity", "maturity_date IS NOT NULL AND maturity_date > start_date")
@expect_or_drop("valid_currency", "ccy IN ('USD','EUR','GBP','JPY','CHF','CAD','AUD')")
@expect_or_drop("valid_counterparty", "counterparty_id IS NOT NULL")
@expect_or_drop("valid_book", "book_id IS NOT NULL")
@expect_or_drop("priceable_irs", "fixed_rate IS NOT NULL AND float_index IS NOT NULL")  -- IRS only
@expect_or_drop("priceable_option", "strike IS NOT NULL AND underlying IS NOT NULL")    -- Options only
```

- Every dropped row is captured in `quarantine_trades` with the failing rule name and raw payload
- Per-product silver tables enforce QuantLib-ready schemas

---

### Stage 10: Data Quality Monitoring (DQX)

**Notebook:** `10_dqx_monitors.py`

**Market Data Checks:**
- Flag any rate, FX rate, or equity price that changes by more than 1 historical standard deviation in a single day
- Compare today's value vs yesterday's value; threshold = hist_vol from `gold_hist_vol`

**Trade Data Checks:**
- Warn on notionals > $10,000,000
- Warn when a trade's notional or other key value changes substantially (> 20%) day-on-day
- Completeness checks on required fields

**Output:** `dq_results` (all check results) and `dq_alerts` (only failures/warnings)

---

### Stages 11–12: Reference Data (Books, Counterparties, Agreements)

Enforced by the trade generator and validated by pipeline expectations:
- Every trade has a `book_id` → `dim_book`
- Every trade has a `counterparty_id` → `dim_counterparty`
- Netting IDs are unique per counterparty (FK constraint in expectations)
- At least 10 books with large trade counts
- At least 1 counterparty with a very large trade set
- ≥1/3 netting agreements = NA, ≥1/3 collateral agreements = NA

---

### Stage 13: Exposure Calculation (PFE)

**Notebook:** `13_calc_exposure.py`

**Pricing Approach:**
- **Grid pricing:** Vectorised NumPy pricers via pandas UDFs on serverless
  - IRS: discounted fixed/float cashflows using simulated curves
  - XCCY: dual-currency cashflow discounting with simulated FX
  - FRA: forward-rate agreement payoff
  - FX Forward: F(T) - K discounted
  - European Options: Black-Scholes with simulated vol
  - American Options: Barone-Adesi-Whaley approximation
- **QuantLib cross-check:** Today's valuation for all trades + spot-check sample (within 1bp / 0.1%)
- **Scale:** ~300k trades × ~44 steps × 1000 paths ≈ 13B valuations (vectorised, not full QuantLib)

**Exposure Aggregation:**
1. **Trades with NA netting agreement:** Exposure = max(PV, 0) per trade
2. **Trades with a netting agreement:** Sum PVs within netting set, then Exposure = max(ΣPV, 0)
3. Aggregate exposures per counterparty per time step across all paths
4. Sort the path distribution and read off:
   - **EE (Expected Exposure):** Mean across paths
   - **PFE (Potential Future Exposure) at 95%:** 95th percentile across paths

**Output:** `gold_cpty_exposure`

---

### Stage 14: VaR Calculation

**Notebook:** `14_calc_var.py`

**Method:**
- Aggregate trade PVs to book level per path per time step
- Compute distribution statistics:
  - Mean PV
  - 5th percentile PV
  - 95th percentile PV
  - **VaR = 95th percentile of the book's loss distribution** (i.e., P95 of negative PV changes)

**Output:** `gold_book_var`

---

### Stage 15: Databricks App + Lakebase

**Lakebase Setup:**
- Gold tables synced to Lakebase (Postgres-compatible) as synced tables
- Tables: `gold_cpty_exposure`, `gold_book_var`, `gold_trade_prices_sample`, `gold_sim_curves`, `gold_sim_fx`, `gold_sim_equity`, `dim_book`, `dim_counterparty`, `dim_netting_agreement`, `dim_collateral_agreement`
- App queries Lakebase for sub-second response times

**App Screens:**

1. **Portfolio Overview**
   - Summary cards: total PFE, total VaR, trade count, quarantine count
   - Top counterparty exposures bar chart
   - Top book VaR bar chart

2. **Counterparty Exposure View**
   - Select counterparty from dropdown
   - PFE profile line chart (time step vs exposure, with EE and PFE bands)
   - Drill-down to netting sets within that counterparty
   - Drill-down to individual trades within a netting set

3. **Netting Set View**
   - Distribution of exposure paths as a fan chart
   - Trade-level contributions to the netting set PV
   - Link to simulated rates that drove each price

4. **Book VaR View**
   - Select book from dropdown
   - VaR distribution chart per time step (mean, P5, P95)
   - Drill-down to trade-level P&L contributions

5. **Market Data Explorer**
   - Simulated curve viewer (select currency, time step, see path distribution)
   - Simulated FX and equity paths
   - Historical vs simulated comparison

6. **Trade Entry Screen**
   - One tab per product type (IRS, XCCY, FRA, FX Forward, European Option, American Option)
   - Dropdowns for counterparty, book, netting agreement (populated from existing dimension tables)
   - Input fields for all trade attributes
   - "Price" button: calls QuantLib to value the trade under current market conditions
   - "Save" button: writes the trade to Lakebase and feeds it back into the bronze layer

---

### Stage 16: Genie Agent

**Genie Space:** `Risk Analyst Analytics`

**Tables Exposed:**
- `gold_cpty_exposure`, `gold_book_var`, `gold_trade_prices_sample`
- `gold_zero_curves`, `gold_sim_curves`, `gold_sim_fx`, `gold_sim_equity`
- `gold_hist_vol`, `gold_corr_matrix`
- `dim_book`, `dim_counterparty`, `dim_netting_agreement`, `dim_collateral_agreement`
- `silver_trades_*` (all 6 product tables)
- `quarantine_trades`, `dq_alerts`

**Metric Definitions (in Genie instructions):**
- **PFE:** 95th percentile of the simulated exposure distribution for a counterparty at a given time step
- **EE:** Mean of the simulated exposure distribution
- **VaR:** 95th percentile of the book-level loss distribution
- **Netting:** For trades under the same netting agreement, net the PVs before flooring at zero
- **Quarantine rate:** count of quarantined trades / count of all ingested trades

**Example Questions:**
- "What is the PFE for counterparty X at the 1-year horizon?"
- "Which books have the highest VaR?"
- "Show me all quarantined trades for FX forwards"
- "What counterparties have exposure exceeding $50M?"
- "How has the USD 10Y zero rate volatility changed over the last 6 months?"

Following your preferences, percentages returned as ratios (0–1) in SQL, formatted in visualisations.

---

### ML/GenAI Intelligence Layer

**Anomaly Detection:**
- Isolation Forest model trained on trade features (notional, maturity, strike/rate relative to market)
- Scored daily on new/amended trades
- Model tracked in MLflow, registered in UC Model Registry
- Anomaly scores written to `gold_trade_anomalies`

**GenAI Risk Commentary:**
- Daily `ai_query` call summarising:
  - Largest PFE movers (counterparty level)
  - Largest VaR changes (book level)
  - DQX alerts triggered
  - Notable market moves (> 1σ)
- Commentary stored in `gold_risk_commentary` and displayed in the app's overview screen

---

## 5. Job Orchestration

### `risk_daily` (Main Daily Job)

```
┌─────────────────────────────────────────────────────────────────┐
│  Task Graph                                                     │
│                                                                 │
│  ingest_fred ──┐                                                │
│  ingest_yf ────┤──→ market_sdp_pipeline ──→ bootstrap_curves    │
│  ingest_fx ────┘                               │                │
│                                          ┌─────┴──────┐        │
│                                    calc_vol    calc_corr*       │
│                                          └─────┬──────┘        │
│                                          mc_simulation          │
│                                                │                │
│  bloomberg_adapter ────────────────────────────→│                │
│                                                │                │
│  generate_trades ──→ trade_sdp_pipeline ──→ dqx_checks          │
│                                                │                │
│                                          ┌─────┴──────┐        │
│                                    calc_exposure  calc_var      │
│                                          └─────┬──────┘        │
│                                     lakebase_sync               │
│                                          │                      │
│                                   genai_commentary              │
│                                          │                      │
│                                   anomaly_scoring               │
└─────────────────────────────────────────────────────────────────┘

* calc_corr runs on Mondays only (weekly calibration)
```

### `setup` (One-Time)
- Schema, tables, volumes, Lakebase, grants

### `backfill` (One-Time, Rerunnable)

Parameters: `backfill_start_date`, `backfill_end_date`, `backfill_chunk_months`. See Stage 1b for details.

```
backfill_fred ──────┐
backfill_yfinance ──┤
backfill_ecb_boe ───┤──→ market_sdp_pipeline (full refresh) ──→ validate_backfill
backfill_oecd ──────┘                                                │
                                                     backfill_zero_curves
                                                                     │
                                                ┌────────────────────┼──────────────────┐
                                         backfill_hist_vol   backfill_corr   backfill_hw_calibration
                                                └────────────────────┼──────────────────┘
                                                          write_watermarks
                                                                     │
                                                          export_backfill_evidence
```

- Source tasks run in parallel; each loops over date chunks and logs to `ctl_backfill_log`
- `validate_backfill` fails the job if a series has gaps over 5 business days or less than 2 years of history
- The job can be rerun for any date range; all writes are idempotent

---

## 6. Pricing Volume & Performance Strategy

**Problem:** ~300k trades × ~44 time steps × 1,000 paths ≈ 13 billion valuations

**Solution:**
1. **Vectorised NumPy pricers** (`pricers_vec.py`) for grid pricing, distributed via pandas UDFs on serverless
2. **QuantLib** (`pricers_ql.py`) used for:
   - Today's valuation (current market, all trades)
   - Cross-check of vectorised prices on a sample (must agree within 1bp / 0.1%)
   - App's live trade pricer
3. **American options:** Barone-Adesi-Whaley approximation (closed-form) instead of binomial trees on the grid
4. **Storage optimisation:** Don't store all 13B trade-level prices. Store:
   - Netting-set and book aggregates for every path (needed for PFE/VaR)
   - Trade-level prices for 50 representative paths only (for drill-down)

---

## 7. Non-USD Curve Data Sources

| Currency | Overnight rate | Curve tenors | Source |
| --- | --- | --- | --- |
| USD | SOFR (FRED) | Treasury CMT 1M–30Y (FRED) | FRED |
| EUR | €STR (FRED ECBESTRVOLWGTTRMDMNRT) | ECB AAA curve | ECB Statistical Data Warehouse (public) |
| GBP | SONIA (FRED IUDSOIA) | BoE gilt curve | Bank of England (public) |
| JPY | Call rate | OECD govt bond yields | OECD Data API (public) |
| CHF | SARON (proxy) | OECD govt bond yields | OECD Data API (public) |
| CAD | CORRA (proxy) | OECD govt bond yields | OECD Data API (public) |

---

## 8. Execution Evidence Requirements

The evaluator reads text only — no images, screenshots, or recordings.

**What gets committed to the GitHub repo:**

| Evidence type | Location | Content |
| --- | --- | --- |
| Notebooks with outputs | `evidence/notebooks/*.ipynb` | Cell outputs from real job runs (tables, counts, sample rows) |
| Job run logs | `evidence/runs/*.md` | Run IDs, task states, durations, error messages (via SDK) |
| Query results | `evidence/results/table_counts.md` | Row counts per table proving >50k per trade type |
| Query results | `evidence/results/backfill_coverage.md` | Per-series date range, row counts, gap checks, spot-value checks |
| Query results | `evidence/results/hw_calibration.md` | Hull-White alpha/sigma history per currency, with fallback cases |
| Query results | `evidence/results/quarantine_stats.md` | Quarantine counts and reasons |
| Query results | `evidence/results/netting_collateral_na.md` | Proof that ≥1/3 are NA |
| Query results | `evidence/results/sample_pfe.md` | Sample PFE and EE rows |
| Query results | `evidence/results/sample_var.md` | Sample VaR rows |
| Query results | `evidence/results/pricer_crosscheck.md` | QuantLib vs vectorised comparison |
| Query results | `evidence/results/dqx_alerts.md` | DQX alert counts and samples |
| Query results | `evidence/results/genie_qa.md` | Genie Q&A transcripts |
| Test output | `evidence/tests.log` | pytest output |
| Model output | `evidence/results/anomaly_scores.md` | Sample anomaly scores |
| GenAI output | `evidence/results/risk_commentary.md` | Sample daily risk commentary |

**Collection script:** `scripts/collect_evidence.py` uses the Databricks SDK to:
1. List recent runs of `risk_daily`, `setup`, and `backfill` jobs
2. Extract task states, durations, and logs
3. Run SQL queries for table counts, sample rows, and statistics
4. Export notebooks with outputs
5. Write everything to `evidence/` as markdown

**Pre-push check:** A git pre-commit hook scans for confidential text and blocks the methodology source file from being committed.

---

## 9. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Methodology file is confidential (Barclays) | Write neutral `docs/methodology.md` describing standard models; never commit source doc |
| Bloomberg data cannot be published | Synthetic adapter is the default; real Bloomberg adapter exists but uses no publishable data |
| 13B valuations too slow for full QuantLib | Vectorised NumPy pricers + QuantLib cross-check on sample |
| Non-USD curves thin on FRED | Supplement with ECB, BoE, OECD public data; document proxies |
| FRED limits ICE BofA OAS history to roughly the last 3 years | Use what is available; extend older history with synthetic spreads from the Bloomberg adapter, flagged as synthetic |
| API rate limits during backfill (FRED, yfinance) | Throttling, batching, retry with backoff, and chunked reruns via `ctl_backfill_log` |
| Gaps or overlap between backfill and daily loads | `ctl_load_watermark` drives incremental loads; silver keyed on (source, series_id, obs_date) |
| American option pricing on grid | Barone-Adesi-Whaley closed-form approximation |
| Evaluator cannot see images | All evidence committed as text (markdown tables, logged output, notebook cell results) |
| Repo must be readable | Public GitHub repo; pre-push hook prevents confidential content |

---

## 10. Technology Stack

| Component | Technology |
| --- | --- |
| Deployment | Declarative Automation Bundles (DABs) |
| Compute | Serverless (all jobs and notebooks) |
| Ingestion | Lakeflow (Auto Loader + SDP pipelines) |
| Governance | Unity Catalog (tables, volumes, secrets, tags, model registry) |
| Curve Bootstrap | QuantLib (`PiecewiseLogCubicDiscount`) |
| Pricing (grid) | Vectorised NumPy + pandas UDFs |
| Pricing (accurate) | QuantLib (today's PV + cross-check) |
| Pricing (app) | QuantLib (live single-trade) |
| Data Quality | DQX |
| ML | Isolation Forest (scikit-learn), MLflow tracking, UC model registry |
| GenAI | `ai_query` for daily risk commentary |
| Operational Serving | Lakebase (synced tables from gold layer) |
| App | Databricks App (Dash or Gradio) |
| NL Queries | Genie Agent |
| Dashboards | AI/BI Dashboards |
| Version Control | GitHub (public repo) |

---

## 11. Build Order (Recommended)

1. Stage 0: Setup job → validate schema and tables exist
2. Stage 1: Market ingestion + SDP pipeline → validate bronze/silver tables populated for a single day
3. Stage 1b: Historic market rates backfill → validate coverage report (≥2Y per series, no gaps > 5 business days) and watermarks written
4. Stage 2: Curve bootstrap → validate `gold_zero_curves` (full history from backfill + latest day)
5. Stage 3: Historical vol → validate `gold_hist_vol`
6. Stage 4: Correlation → validate `gold_corr_matrix` and `gold_hw_params`
7. Stage 5: MC simulation → validate simulation tables
8. Stage 8: Trade generator → validate trade files in volume
9. Stage 9: Trade SDP pipeline → validate silver trade tables + quarantine
10. Stages 11–12: Reference data → validate dimensions
11. Stage 6: Bloomberg adapter → validate bond/credit tables
12. Stage 10: DQX → validate alerts
13. Stage 13: Exposure → validate `gold_cpty_exposure`
14. Stage 14: VaR → validate `gold_book_var`
15. Stage 7: Dashboards → visual review
16. Stage 15: App + Lakebase → end-to-end test
17. Stage 16: Genie space → Q&A validation
18. ML/GenAI → anomaly + commentary
19. Evidence collection → commit to repo
20. DABs packaging → `databricks bundle validate` + `deploy`
