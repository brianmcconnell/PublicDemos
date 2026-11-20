## Stage: 13_calc_exposure

* started (UTC): `2026-11-20 11:08:48`
* finished (UTC): `2026-11-20 11:13:30`  (281.4s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| what-if counterparty files | PASS | 300 files, 300 counterparties |
| all clean trades binned | PASS | 167083 trades |
| exposure computed | PASS | 300 counterparties |
| VaR computed | PASS | 25 books |
| steps per cpty | PASS | 46 steps |
* as_of: `2026-11-20`
* cube_path: `/Volumes/serverless_stable_7s0j2u_catalog/market/sim_cubes/cube_2026-11-20.npz`
* total trades: `167829`
* clean trades: `167083`
* bins: `668`
* pricing seconds: `197`
* exposure rows: `44160`
* counterparties: `300`

**Top 10 counterparties by peak PFE95 (USD)**

| counterparty_id | peak_pfe_95 | peak_ee | n_trades |
| --- | --- | --- | --- |
| CP0001 | 1.6039e+09 | 1.44826e+09 | 34004 |
| CP0002 | 9.66792e+08 | 7.42791e+08 | 15746 |
| CP0007 | 7.65944e+08 | 7.03897e+08 | 4085 |
| CP0008 | 7.60023e+08 | 6.98748e+08 | 3406 |
| CP0003 | 5.90077e+08 | 4.88072e+08 | 10111 |
| CP0004 | 5.27375e+08 | 3.98627e+08 | 7266 |
| CP0006 | 5.19236e+08 | 3.03945e+08 | 4715 |
| CP0014 | 4.38531e+08 | 3.9963e+08 | 1893 |
| CP0005 | 3.43136e+08 | 2.48924e+08 | 5747 |
| CP0016 | 3.29168e+08 | 3.144e+08 | 1607 |
* var rows: `1150`
* books: `25`

**1-day VaR95 (top 10 books, USD)**

| book_id | time_step | n_trades | t0_pv | mean_pv | p5_pv | p95_pv | var_95 | expected_shortfall_95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BK001 | 1 | 19169 | 1.39392e+08 | 1.42002e+08 | 1.1908e+08 | 1.66416e+08 | 2.03126e+07 | 2.55259e+07 |
| BK006 | 1 | 7914 | 6.41364e+07 | 5.39618e+07 | 4.55069e+07 | 6.27455e+07 | 1.86295e+07 | 2.08538e+07 |
| BK017 | 1 | 4714 | -1.62998e+06 | -3.21391e+06 | -1.66391e+07 | 9.67105e+06 | 1.50091e+07 | 1.8528e+07 |
| BK003 | 1 | 11201 | -6.70888e+07 | -6.56197e+07 | -8.0204e+07 | -5.02609e+07 | 1.31151e+07 | 1.64763e+07 |
| BK014 | 1 | 5099 | 6.27517e+07 | 6.30316e+07 | 5.1649e+07 | 7.48656e+07 | 1.11027e+07 | 1.40191e+07 |
| BK008 | 1 | 6763 | 5.92597e+07 | 5.77734e+07 | 4.84379e+07 | 6.64942e+07 | 1.08218e+07 | 1.29247e+07 |
| BK015 | 1 | 4931 | -1.76883e+08 | -1.79585e+08 | -1.8717e+08 | -1.72102e+08 | 1.02873e+07 | 1.24892e+07 |
| BK016 | 1 | 4906 | -2.45744e+07 | -2.61514e+07 | -3.47394e+07 | -1.74499e+07 | 1.0165e+07 | 1.21612e+07 |
| BK022 | 1 | 4101 | -1.03025e+07 | -1.17634e+07 | -2.02651e+07 | -3.48571e+06 | 9.96266e+06 | 1.20154e+07 |
| BK013 | 1 | 5324 | -1.70239e+07 | -2.09038e+07 | -2.61332e+07 | -1.54308e+07 | 9.10935e+06 | 1.02553e+07 |
* what-if portfolio dir: `/Volumes/serverless_stable_7s0j2u_catalog/market/sim_cubes/portfolio_2026-11-20`
* sample rows: `109480`
