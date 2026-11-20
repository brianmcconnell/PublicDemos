## Stage: 05_mc_simulation

* started (UTC): `2026-11-20 11:07:14`
* finished (UTC): `2026-11-20 11:08:29`  (74.7s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| simulation ran | PASS | 46 steps |
| sim equity written | PASS | 2208000 rows |
| sim rates written | PASS | 5520000 rows (expected 5520000) |
| rebuilt curve reprices t0 | PASS | max abs error 5.34e-16 |
| sim fx written | PASS | 230000 rows (expected 230000) |
| sim fx starts at spot | PASS | max abs error 0.00e+00 |
* as_of: `2026-11-20`
* n_paths: `1000`
* cube_path: `/Volumes/serverless_stable_7s0j2u_catalog/market/sim_cubes/cube_2026-11-20.npz`
* n_steps: `46`
* n_paths: `1000`
* sim_equity_rows: `2208000`
* sim_rate_rows: `5520000`
* sim_rate_t0_max_abs_error: `5.34e-16`
* sim_fx_rows: `230000`
