# Derivatives Risk Platform — Execution Evidence

Generated: 2026-11-24 19:41:37 UTC

---

## Stage: 01_ingest_fred

* started (UTC): `2026-11-24 19:27:04`
* finished (UTC): `2026-11-24 19:27:34`  (30.3s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| rows loaded | PASS | 0 rows |
* date range: `2026-11-24 to 2026-11-23`
* n_series: `41`
* rows fetched: `0`
* failed series: `['DGS1MO', 'DGS3MO', 'DGS6MO', 'DGS1', 'DGS2', 'DGS3', 'DGS5', 'DGS7', 'DGS10', 'DGS20', 'DGS30', 'SOFR', 'DFF', 'ECBESTRVOLWGTTRMDMNRT', 'IUDSOIA', 'IR3TIB01EZM156N', 'IRLTLT01EZM156N', 'IR3TIB01GBM156N', 'IRLTLT01GBM156N', 'IR3TIB01JPM156N', 'IRLTLT01JPM156N', 'IR3TIB01CHM156N', 'IRLTLT01CHM156N', 'IR3TIB01CAM156N', 'IRLTLT01CAM156N', 'DEXUSEU', 'DEXUSUK', 'DEXJPUS', 'DEXSZUS', 'DEXCAUS', 'BAMLC0A1CAAA', 'BAMLC0A2CAA', 'BAMLC0A3CA', 'BAMLC0A4CBBB', 'BAMLH0A1HYBB', 'BAMLH0A2HYB', 'BAMLH0A3HYC', 'DFII5', 'DFII10', 'T5YIE', 'T10YIE']`

---

## Stage: 01_ingest_yfinance

* started (UTC): `2026-11-24 19:27:53`
* finished (UTC): `2026-11-24 19:28:09`  (15.9s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| equity data | FAIL | 0 rows |
| fx up to date | PASS | oldest latest-FX date is 0 days old (60 FRED, 10 Frankfurter rows loaded) |
* equity rows: `0`
* fx window: `2026-10-10 to 2026-11-24`
* fx rows FRED: `60`
* fx rows Frankfurter: `10`

---

## Stage: 02_bootstrap_curves

* started (UTC): `2026-11-24 19:28:27`
* finished (UTC): `2026-11-24 19:28:40`  (13.6s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| USD curve | PASS | bootstrap ran |
| all currencies | PASS | 6 currencies |
* as_of: `2026-11-24`
* curve_date: `2026-11-20`
* curve rows: `42`

---

## Stage: 03_calc_hist_vol

* started (UTC): `2026-11-24 19:28:59`
* finished (UTC): `2026-11-24 19:29:20`  (21.2s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| vol table populated | PASS | 361 rows |
* vol rows: `361`

---

## Stage: 04_calc_correlation

* started (UTC): `2026-11-24 19:29:39`
* finished (UTC): `2026-11-24 19:29:53`  (14.2s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| corr matrix PSD | PASS | 17 factors |
* matrix size: `17`
* rows written: `3481`

---

## Stage: 05_mc_simulation

* started (UTC): `2026-11-24 19:30:12`
* finished (UTC): `2026-11-24 19:31:06`  (54.0s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| simulation ran | PASS | 46 steps |
| sim equity written | PASS | 2208000 rows |
| sim rates written | PASS | 5520000 rows (expected 5520000) |
| rebuilt curve reprices t0 | PASS | max abs error 6.25e-16 |
| sim fx written | PASS | 230000 rows (expected 230000) |
| sim fx starts at spot | PASS | max abs error 0.00e+00 |
* as_of: `2026-11-24`
* n_paths: `1000`
* cube_path: `/Volumes/serverless_stable_7s0j2u_catalog/market/sim_cubes/cube_2026-11-24.npz`
* n_steps: `46`
* n_paths: `1000`
* sim_equity_rows: `2208000`
* sim_rate_rows: `5520000`
* sim_rate_t0_max_abs_error: `6.25e-16`
* sim_fx_rows: `230000`

---

## Stage: 06_bloomberg_adapter

* started (UTC): `2026-11-24 19:27:02`
* finished (UTC): `2026-11-24 19:27:24`  (21.8s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| synthetic data written | PASS |  |
* bonds: `30`
* spreads: `20`

---

## Stage: 08_generate_trades

* started (UTC): `2026-10-22 15:13:46`
* finished (UTC): `2026-10-22 15:14:54`  (67.9s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| trades generated | PASS | >=300k |
| defects injected | PASS | 600 |
* total clean: `312000`
* total defects: `600`

---

## Stage: 09_trade_lifecycle

* started (UTC): `2026-11-24 19:28:59`
* finished (UTC): `2026-11-24 19:32:17`  (198.0s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| nothing to do | PASS | no trading day after 2026-11-20 |
* mode: `incremental`
* trading days: `0`

---

## Stage: 10_dqx_monitors

* started (UTC): `2026-11-24 19:32:41`
* finished (UTC): `2026-11-24 19:32:55`  (14.3s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| irs quarantine < 5% | PASS | 284/150418 = 0.19% |
| xccy quarantine < 5% | PASS | 38/19513 = 0.19% |
| fra quarantine < 5% | PASS | 65/16707 = 0.39% |
| fxfwd quarantine < 5% | PASS | 2187/491561 = 0.44% |
| euro_opt quarantine < 5% | PASS | 299/68668 = 0.44% |
| amer_opt quarantine < 5% | PASS | 93/25758 = 0.36% |
* irs notional > 500M: `0`
* xccy notional > 500M: `0`
* fra notional > 500M: `0`
* fxfwd notional > 500M: `0`
* euro_opt notional > 500M: `0`
* amer_opt notional > 500M: `0`
* bronze_fred_rates >10% moves: `2870`
* bronze_equity_prices >10% moves: `307`

---

## Stage: 13_calc_exposure

* started (UTC): `2026-11-24 19:33:11`
* finished (UTC): `2026-11-24 19:39:25`  (373.8s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| what-if counterparty files | PASS | 300 files, 300 counterparties |
| all clean trades binned | PASS | 166198 trades |
| exposure computed | PASS | 300 counterparties |
| VaR computed | PASS | 25 books |
| steps per cpty | PASS | 46 steps |
* as_of: `2026-11-24`
* cube_path: `/Volumes/serverless_stable_7s0j2u_catalog/market/sim_cubes/cube_2026-11-24.npz`
* netted_trades: `0`
* unnetted_trades: `166198`
* total trades: `166941`
* clean trades: `166198`
* bins: `664`
* pricing seconds: `299`
* exposure rows: `44160`
* counterparties: `300`

**Top 10 counterparties by peak PFE95 (USD)**

| counterparty_id | peak_pfe_95 | peak_ee | n_trades |
| --- | --- | --- | --- |
| CP0001 | 1.56655e+09 | 1.38878e+09 | 33805 |
| CP0002 | 9.08747e+08 | 7.08464e+08 | 15680 |
| CP0007 | 7.18389e+08 | 6.6866e+08 | 4067 |
| CP0008 | 6.91501e+08 | 6.48579e+08 | 3376 |
| CP0003 | 5.82605e+08 | 4.55496e+08 | 10084 |
| CP0004 | 5.33372e+08 | 3.4006e+08 | 7224 |
| CP0006 | 5.08978e+08 | 2.85026e+08 | 4683 |
| CP0014 | 4.24769e+08 | 3.86428e+08 | 1881 |
| CP0005 | 3.4165e+08 | 2.4276e+08 | 5714 |
| CP0016 | 3.23315e+08 | 3.07748e+08 | 1597 |
* var rows: `1150`
* books: `25`

**1-day VaR95 (top 10 books, USD)**

| book_id | time_step | n_trades | t0_pv | mean_pv | p5_pv | p95_pv | var_95 | expected_shortfall_95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BK016 | 1 | 4889 | -2.28995e+07 | -4.63719e+07 | -5.13711e+07 | -4.11986e+07 | 2.84716e+07 | 2.9523e+07 |
| BK001 | 1 | 19070 | 1.97031e+08 | 1.9277e+08 | 1.80439e+08 | 2.05001e+08 | 1.6592e+07 | 2.04386e+07 |
| BK003 | 1 | 11141 | -8.75626e+07 | -8.81028e+07 | -9.7107e+07 | -7.89436e+07 | 9.54441e+06 | 1.18911e+07 |
| BK006 | 1 | 7884 | 8.77278e+07 | 8.46608e+07 | 7.94095e+07 | 8.97597e+07 | 8.31836e+06 | 9.44032e+06 |
| BK008 | 1 | 6730 | 6.38265e+07 | 6.28083e+07 | 5.7444e+07 | 6.79372e+07 | 6.38244e+06 | 8.06763e+06 |
| BK014 | 1 | 5072 | 2.90378e+07 | 3.02298e+07 | 2.34205e+07 | 3.69303e+07 | 5.61734e+06 | 7.23967e+06 |
| BK007 | 1 | 7189 | 1.39812e+08 | 1.38983e+08 | 1.34381e+08 | 1.43317e+08 | 5.43158e+06 | 6.86151e+06 |
| BK023 | 1 | 4072 | -791,614 | -1.04843e+06 | -6.20233e+06 | 3.68765e+06 | 5.41071e+06 | 6.63759e+06 |
| BK017 | 1 | 4687 | 3.30815e+07 | 3.60492e+07 | 2.79593e+07 | 4.4264e+07 | 5.12217e+06 | 7.2713e+06 |
| BK009 | 1 | 6553 | -5.7521e+06 | -5.89023e+06 | -1.08488e+07 | -617,706 | 5.09668e+06 | 6.51533e+06 |
* what-if portfolio dir: `/Volumes/serverless_stable_7s0j2u_catalog/market/sim_cubes/portfolio_2026-11-24`
* sample rows: `109480`

---

## Stage: 15_calc_sensitivities

* started (UTC): `2026-11-24 19:39:42`
* finished (UTC): `2026-11-24 19:41:19`  (97.0s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| every trade has an MTM | PASS | 166198 of 166198 |
| hierarchy TOTAL = sum of trades | PASS | 277,671,039 |
| hierarchy books = TOTAL | PASS | 277,671,039 |
| sum of key rates ~ parallel DV01 (per trade, vs gross DV01) | PASS | rel diff 8.2e-05 |
| VaR history recorded | PASS | 1150 rows, as_of 2026-11-24 |
* run_id: `668435503052857`
* as_of: `2026-11-24`
* trades: `166198`
* pricing seconds: `62`
* gold_trade_mtm_history: `166198`
* gold_trade_sensitivity_history: `1144621`
* gold_risk_hierarchy_history: `7883`
* gold_market_snapshot_history: `347`
* gold_var_history: `1150`
* var_as_of_date: `2026-11-24`
* gross |DV01| (USD): `390501053`
* net key rates - net DV01 (USD): `-47`

**Total sensitivities (USD)**

| measure | value_usd |
| --- | --- |
| MTM | 2.77671e+08 |
| IR_KRD | 143,496 |
| EQ_VEGA | 1.87733e+06 |
| IR_DV01 | 143,543 |
| FX_DELTA | -5.5212e+07 |
| EQ_GAMMA | 74,200 |
| EQ_DELTA | 2.46212e+06 |

---

