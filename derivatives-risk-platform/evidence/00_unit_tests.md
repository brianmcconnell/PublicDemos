## Stage: unit tests and library validation (riskcore)

* Environment: Databricks serverless, Python 3.12, QuantLib 1.44, pandas 2.3.3, numpy 2.3.4
* Command: `python -B -m pytest -q -p no:cacheprovider tests/test_riskcore.py`
* Result: **18 passed in 6.40s**
* Market data: synthetic (USD par Treasury curve, flat-ish non-USD zero curves, constant vol term structures) — no tables required

### Vectorised grid pricer vs QuantLib at t0 (60 trades per product, 500 paths)

| product | n | QuantLib NaN | max abs diff (USD) | max diff (bp of notional) | mean abs PV (USD) |
| --- | --- | --- | --- | --- | --- |
| irs | 60 | 0 | 0.0032 | 0.0000084 | 23,635 |
| xccy | 60 | 0 | 1.73 | 0.00044 | 1,055,323 |
| fra | 60 | 0 | 0.00068 | 0.00000058 | 5,110 |
| fxfwd | 60 | 0 | 1.63 | 0.00048 | 212,072 |
| euro_opt | 60 | 0 | 0.078 | 0.000099 | 266,032 |
| amer_opt | 60 | 0 | 1.19 | 0.0041 | 226,076 |

Residual differences come from float32 storage of the simulated cube and the BAW Newton tolerance.

### Hull-White martingale check (USD, 500 paths): E[D(0,T)] vs P(0,T)

| T (years) | E[D] | P0(T) |
| --- | --- | --- |
| 0.04 | 0.99835 | 0.99835 |
| 0.58 | 0.97692 | 0.97692 |
| 2.00 | 0.93128 | 0.93114 |
| 10.01 | 0.67061 | 0.66951 |

### Hull-White calibration cases

| inputs (vol1, vol10, n_obs) | case | alpha | sigma |
| --- | --- | --- | --- |
| 0.009, 0.03, 2000 | 7 (calibrated) | 0.3429 | 0.01063 |
| 0.006, 0.055, 2000 | 5 (alpha floored) | 0.1 | 0.00870 |
| 0.006, 0.055, 100 | 3 (insufficient history) | fallback | fallback |
| 0.0, 0.05, 2000 | 1 (zero vol) | fallback | fallback |
| 0.06, 0.05, 2000 | 2 (ratio > 1) | fallback | fallback |

### Exposure aggregation

* Hand-checked case (2 netted trades + 1 un-netted trade, 4 paths): EE = 3.0 (expected 3.0), PFE95 = 5.7 (expected 5.7)
* Distributed (3 random bins, partial sums) vs single-pass: 9,614 exposure rows matched; max |EE diff| 3.7e-09, max |PFE diff| 9.3e-09, max |VaR diff| 7.5e-09 USD

### Trade population rules (2,400-trade sample)

* Netting set 'NA' share: 0.352 (>= 1/3 required)
* Collateral 'NA' share: 0.725 (>= 1/3 required)
* Netting set ids shared across counterparties: 0
* Largest counterparty CP0001: 485 trades (~20%); top 10 books each 87–276 trades

### Performance (1 core, 1,000 paths x 46 steps, per 100 trades)

| product | seconds |
| --- | --- |
| irs | 1.73 |
| xccy | 1.48 |
| fra | 0.14 |
| fxfwd | 0.17 |
| euro_opt | 0.56 |
| amer_opt | 4.35 (was 55.8 before the branch-split / early-stop BAW rewrite) |

Estimated full book (~312k trades): ~70 core-minutes, distributed via load-balanced bins.
