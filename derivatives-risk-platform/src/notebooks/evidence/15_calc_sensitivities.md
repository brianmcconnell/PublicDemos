## Stage: 15_calc_sensitivities

* started (UTC): `2026-11-20 11:13:47`
* finished (UTC): `2026-11-20 11:16:25`  (158.0s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| every trade has an MTM | PASS | 167083 of 167083 |
| hierarchy TOTAL = sum of trades | PASS | 206,069,438 |
| hierarchy books = TOTAL | PASS | 206,069,438 |
| sum of key rates ~ parallel DV01 (per trade, vs gross DV01) | PASS | rel diff 8.3e-05 |
| VaR history recorded | PASS | 1150 rows, as_of 2026-11-20 |
* run_id: `788649149658955`
* as_of: `2026-11-20`
* trades: `167083`
* pricing seconds: `105`
* gold_trade_mtm_history: `167083`
* gold_trade_sensitivity_history: `1085677`
* gold_risk_hierarchy_history: `7566`
* gold_market_snapshot_history: `337`
* gold_var_history: `1150`
* var_as_of_date: `2026-11-20`
* gross |DV01| (USD): `397237531`
* net key rates - net DV01 (USD): `4`

**Total sensitivities (USD)**

| measure | value_usd |
| --- | --- |
| EQ_GAMMA | 59,773 |
| IR_KRD | 73,447 |
| MTM | 2.06069e+08 |
| EQ_DELTA | 2.14375e+06 |
| IR_DV01 | 73,443 |
| EQ_VEGA | 1.72289e+06 |
| FX_DELTA | -5.27711e+07 |
