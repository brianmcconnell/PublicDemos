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
