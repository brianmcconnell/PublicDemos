# risk-curves-app: Requirements

Databricks App (`risk-curves-app`) for viewing historical yield curves and the distribution of simulated rates
from the derivatives risk platform's Hull-White 1F Monte Carlo.

## R1. Historical yield curves
* User picks a curve (currency) and a past as-of date; the app shows the zero curve for that date.
* Pickers are populated from Lakebase: `riskapp.ref_currency` (curves, date range) and `riskapp.ref_curve_date`.
* Curve values come from Lakebase `riskapp.zero_curve_history` (synced from `market.gold_zero_curves`).
* Tenors not on the bootstrap grid are shown by linear interpolation in zero rate (same rule as `riskcore.curves.ZeroCurve`)
  and are visually marked as interpolated.

## R2. Distribution of simulated rates
* User picks a rate (currency + tenor, or the short rate) and a future simulation date.
* Simulation dates come from `riskapp.ref_sim_step`; rates from `riskapp.ref_rate` / the active tenor list (R3).
* Every tenor is read off the curve rebuilt on each path from the simulated HW1F state x(t):
  `P(t, t+tau | x) = A(t, t+tau) exp(-B(tau) x)`, `zero = -ln P / tau`; the short rate is `x(t) + phi(t)`.
  This covers tenors the model does not simulate directly.
* Shows a histogram and summary statistics (mean, sd, P1/P5/P25/P50/P75/P95/P99) across the paths, plus a percentile
  fan across all simulation dates for the selected rate.

## R3. User-maintained tenor list (add / remove / edit)
* Users can add, remove and edit tenors (label and maturity in years) from within the app.
* The 12 bootstrap-grid tenors (`riskcore.config.TENORS`) are curve inputs: they are always present and cannot be
  removed or have their maturity changed. Off-grid tenors are fully editable.
* Validation: label matches `^[0-9]+[MY]$` and is unique; maturity in (0, 50] years, unique, and consistent with the
  label (e.g. 18M = 1.5); a soft cap of 40 tenors in total.
* Changes take effect in the app immediately for both R1 and R2, without rerunning the simulation:
  the app computes any active tenor on the fly from the stored HW1F state, so it does not depend on the precomputed
  tenors in `gold_sim_curves`.
* Every change is audited (who, when, old value, new value). Removing a tenor is a soft delete (`is_active = false`),
  so it can be restored.
* Only authorised users can edit (an editors group, checked from the app's forwarded user identity); everyone else
  sees the list read-only.

### R3 design consequences
* **Writable storage in Lakebase.** Synced tables (`riskapp.*`) are read-only copies of the lakehouse and are
  overwritten by the sync, so user edits go in a separate writable schema, `riskapp_cfg`:
  * `riskapp_cfg.tenor_config` (tenor_label PK, tenor_years, on_curve_grid, is_active, sort_order, updated_by, updated_at)
  * `riskapp_cfg.tenor_config_audit` (change_id, tenor_label, action ADD/EDIT/REMOVE/RESTORE, old_*, new_*, changed_by, changed_at)
  * Seeded from `riskcore.config.SIM_RATE_TENORS`; the app's service principal gets read/write on `riskapp_cfg` only.
* **Simulated state the app needs** (written by 05 alongside `gold_sim_curves`):
  * x(t) per currency, time step and path (or derived from the stored short rate as x = r - phi(t)), and
  * the simulation's initial curve and HW parameters (alpha, sigma) per currency.
  These are small (6 x 46 x 1,000 states) and are synced to Lakebase or read through a SQL warehouse.
* **Feeding edits back to the lakehouse.** 05 and 06 read the active tenor list from `riskapp_cfg.tenor_config`
  (through a Unity Catalog catalog registered on the Lakebase database) and fall back to
  `riskcore.config.SIM_RATE_TENORS` if it is unavailable, so `ref_tenor`, `ref_rate` and `gold_sim_curves` follow the
  user's list on the next daily run.

## R4. Data and sync
* Static and reference data, curve history and price histories are held in Lakebase project `riskapp`
  (database `databricks_postgres`, schema `riskapp`) as TRIGGERED synced tables from the lakehouse, refreshed by
  the daily job (`build_app_reference` then `refresh_lakebase_sync`).
* Large simulated path data stays in the lakehouse (`market.gold_sim_curves`, partitioned by as_of_date and ccy) and
  is read through a SQL warehouse when not computed on the fly.
