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
