"""05_mc_simulation: run Monte Carlo simulation and save the cube to a UC volume.

Reads gold_zero_curves, gold_hist_vol, gold_corr_matrix, gold_hw_params (or calibrates on the fly),
bronze_fx_rates, bronze_equity_prices. Writes the cube to /Volumes/.../sim_cubes/cube_{as_of}.npz
and a sample of simulated curves/FX/equity to gold_sim_* tables.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import (cfg_from_widgets, widget, CCYS, FX_CCYS, COUNTRY_INDEX, SINGLE_NAMES, EQUITY_CCY,
                             PENCE_TICKERS, SIM_RATE_TENORS, SHORT_RATE_LABEL)
from riskcore.curves import ZeroCurve
from riskcore.vol import vol_term_structure
from riskcore.corr import core_matrix_from_long, core_factor_names
from riskcore.hullwhite import calibrate, sigma_from_alpha, ALPHA_MIN
from riskcore.sim import MarketState, simulate
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
n_paths = int(widget(dbutils, "n_paths", "1000"))
ev = Evidence("05_mc_simulation")
as_of = dt.date.today()
ev.metric("as_of", str(as_of))
ev.metric("n_paths", n_paths)

# COMMAND ----------
# 1. Load zero curves
zc = spark.sql(f"""
    SELECT ccy, tenor_label, tenor_years, zero_rate FROM {cfg.t('gold_zero_curves')}
    WHERE as_of_date = (SELECT MAX(as_of_date) FROM {cfg.t('gold_zero_curves')})
""").toPandas()
zeros = {}
for ccy, g in zc.groupby("ccy"):
    zeros[ccy] = dict(zip(g.tenor_label, g.zero_rate))
print("Curve currencies:", list(zeros))

# COMMAND ----------
# 2. Load latest FX spot
fx_df = spark.sql(f"""
    WITH latest AS (SELECT ccy, MAX(obs_date) AS d FROM {cfg.t('bronze_fx_rates')} GROUP BY ccy)
    SELECT f.ccy, f.value FROM {cfg.t('bronze_fx_rates')} f JOIN latest l ON f.ccy = l.ccy AND f.obs_date = l.d
""").toPandas()
fx_spot = dict(zip(fx_df.ccy, fx_df.value))
for c in FX_CCYS:
    fx_spot.setdefault(c, 1.0)
print("FX spot:", fx_spot)

# COMMAND ----------
# 3. Load latest equity spot
eq_df = spark.sql(f"""
    WITH latest AS (SELECT ticker, MAX(trade_date) AS d FROM {cfg.t('bronze_equity_prices')} GROUP BY ticker)
    SELECT e.ticker, e.adj_close FROM {cfg.t('bronze_equity_prices')} e JOIN latest l ON e.ticker = l.ticker AND e.trade_date = l.d
""").toPandas()
eq_spot = dict(zip(eq_df.ticker, eq_df.adj_close))
for t in PENCE_TICKERS:
    if t in eq_spot:
        eq_spot[t] /= 100.0
print(f"Equity spot: {len(eq_spot)} tickers")

# COMMAND ----------
# 4. Load vols and build term structures
vol_df = spark.sql(f"""
    SELECT factor_name, window, ann_vol FROM {cfg.t('gold_hist_vol')}
    WHERE calc_date = (SELECT MAX(calc_date) FROM {cfg.t('gold_hist_vol')})
""").toPandas()
# Pivot from long (factor_name, window, ann_vol) to wide (one row per factor with vol_3m, vol_1y, ...)
vol_wide = vol_df.pivot_table(index="factor_name", columns="window", values="ann_vol").reset_index()
eq_vol, fx_vol = {}, {}
for _, r in vol_wide.iterrows():
    ts = vol_term_structure(r.to_dict(), floor=0.03)
    name = r.factor_name
    if name.startswith("EQ_"):
        eq_vol[name[3:]] = ts
    elif name.startswith("FX_"):
        fx_vol[name[3:]] = ts
for tkr in eq_spot:
    eq_vol.setdefault(tkr, {0.25: 0.22, 1.0: 0.21, 3.0: 0.20, 10.0: 0.20})
for c in FX_CCYS:
    fx_vol.setdefault(c, {0.25: 0.08, 1.0: 0.085, 3.0: 0.09, 10.0: 0.09})
print(f"Vol terms: eq {len(eq_vol)}, fx {len(fx_vol)}")

# COMMAND ----------
# 5. HW calibration (from vol table or on the fly)
hw = {}
for ccy in CCYS:
    ir_1y = vol_df[(vol_df.factor_name == f"IR_{ccy}_DF1Y") & (vol_df.window == "vol_1y")]
    ir_10y = vol_df[(vol_df.factor_name == f"IR_{ccy}_DF10Y") & (vol_df.window == "vol_10y")]
    v1 = float(ir_1y.ann_vol.iloc[0]) if len(ir_1y) else None
    v10 = float(ir_10y.ann_vol.iloc[0]) if len(ir_10y) else None
    nobs = int(vol_df[(vol_df.factor_name == f"IR_{ccy}_DF1Y")].shape[0] * 252) if v10 else 0
    cal = calibrate(v1, v10, nobs)
    hw[ccy] = (cal["alpha"], cal["sigma"])
    print(f"HW {ccy}: alpha={cal['alpha']:.4f} sigma={cal['sigma']:.6f} case={cal['calibration_case']}")

# Write gold_hw_params
hw_rows = []
for ccy in CCYS:
    cal = calibrate(
        float(vol_df[(vol_df.factor_name == f"IR_{ccy}_DF1Y") & (vol_df.window == "vol_1y")].ann_vol.iloc[0]) if len(vol_df[(vol_df.factor_name == f"IR_{ccy}_DF1Y") & (vol_df.window == "vol_1y")]) else None,
        float(vol_df[(vol_df.factor_name == f"IR_{ccy}_DF10Y") & (vol_df.window == "vol_10y")].ann_vol.iloc[0]) if len(vol_df[(vol_df.factor_name == f"IR_{ccy}_DF10Y") & (vol_df.window == "vol_10y")]) else None,
        0)
    hw_rows.append(dict(as_of_date=as_of, ccy=ccy, **cal))
spark.createDataFrame(pd.DataFrame(hw_rows)).createOrReplaceTempView("_hw_stg")
spark.sql(f"""
    MERGE INTO {cfg.t('gold_hw_params')} t
    USING _hw_stg s ON t.as_of_date = s.as_of_date AND t.ccy = s.ccy
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

# COMMAND ----------
# 6. Load correlation matrix
corr_df = spark.sql(f"""
    SELECT factor_i, factor_j, correlation FROM {cfg.t('gold_corr_matrix')}
    WHERE calc_date = (SELECT MAX(calc_date) FROM {cfg.t('gold_corr_matrix')})
""").toPandas()
if len(corr_df):
    core_names = set(core_factor_names())
    corr_df["is_core"] = corr_df["factor_i"].isin(core_names) & corr_df["factor_j"].isin(core_names)
    C = core_matrix_from_long(corr_df)
else:
    names = core_factor_names()
    C = pd.DataFrame(np.eye(len(names)), index=names, columns=names)
    print("WARN: no corr data, using identity")
print(f"Correlation matrix: {C.shape}")

# COMMAND ----------
# 7. Single-name betas
sn_beta = {}
for tkr, ccy in SINGLE_NAMES.items():
    if tkr in eq_spot:
        sn_beta[tkr] = 0.65
print(f"Single-name betas: {len(sn_beta)}")

# COMMAND ----------
# 8. Simulate
ms = MarketState(as_of, zeros, hw, fx_spot, fx_vol, eq_spot, eq_vol, sn_beta, C)
cube = simulate(ms, n_paths=n_paths)
print(f"Cube: {cube.n_steps} steps, {cube.n_paths} paths")

# Save to volume (write locally first, then copy — works around serverless I/O limits)
import shutil, tempfile
cube_path = f"{cfg.vol('sim_cubes')}/cube_{as_of}.npz"
with tempfile.NamedTemporaryFile(suffix=".npz", delete=False) as tmp:
    tmp_path = tmp.name
cube.save(tmp_path)
shutil.copy2(tmp_path, cube_path)
os.remove(tmp_path)
print(f"Saved cube to {cube_path}")
ev.metric("cube_path", cube_path)
ev.metric("n_steps", cube.n_steps)
ev.metric("n_paths", cube.n_paths)
ev.check("simulation ran", cube.n_steps == 46, f"{cube.n_steps} steps")

# COMMAND ----------
# 9. Write simulated equity prices to gold_sim_equity
# cube.eq: dict[ticker] -> np.ndarray(n_steps, n_paths)
import itertools

eq_rows = []
for tkr, arr in cube.eq.items():
    n_s, n_p = arr.shape
    steps = np.repeat(np.arange(n_s, dtype=np.int32), n_p)
    paths = np.tile(np.arange(n_p, dtype=np.int32), n_s)
    prices = arr.ravel()
    eq_rows.append(pd.DataFrame({
        "ticker": tkr,
        "time_step": steps,
        "path_id": paths,
        "price": prices,
    }))
eq_pdf = pd.concat(eq_rows, ignore_index=True)
eq_pdf["as_of_date"] = as_of
print(f"gold_sim_equity: building {len(eq_pdf)} rows ({len(cube.eq)} tickers × {cube.n_steps} steps × {cube.n_paths} paths)")

sdf = spark.createDataFrame(eq_pdf[["as_of_date", "ticker", "time_step", "path_id", "price"]])
(sdf.write
    .mode("overwrite")
    .option("replaceWhere", f"as_of_date = '{as_of}'")
    .saveAsTable(cfg.t("gold_sim_equity")))
n_written = spark.table(cfg.t("gold_sim_equity")).filter(f"as_of_date = '{as_of}'").count()
print(f"gold_sim_equity: {n_written} rows written")
ev.metric("sim_equity_rows", n_written)
ev.check("sim equity written", n_written > 0, f"{n_written} rows")

# COMMAND ----------
# 10. Write simulated rates to gold_sim_curves (as_of_date, ccy, time_step, path_id, tenor_label, zero_rate)
# Every tenor is read off the curve rebuilt on each path from the simulated HW1F state x(t):
#   P(t,t+tau | x) = A(t,t+tau) exp(-B(tau) x),  zero = -ln P / tau
# tenor_label SHORT_RATE_LABEL ('SHORT') holds the short rate r(t) = x(t) + phi(t).
# Tenors come from riskcore.config.SIM_RATE_TENORS (shared with 06_build_app_reference / ref_tenor).
labels = list(SIM_RATE_TENORS)
taus = np.array([SIM_RATE_TENORS[k] for k in labels])
n_s, n_p = cube.n_steps, cube.n_paths
steps = np.repeat(np.arange(n_s, dtype=np.int32), n_p)
paths = np.tile(np.arange(n_p, dtype=np.int32), n_s)

rate_sdf, t0_err = None, 0.0
for ccy, hwm in cube.hw.items():
    Z = np.stack([hwm.zero_rates(float(cube.t[k]), taus, cube.x[ccy][k]) for k in range(n_s)])  # (S, L, P)
    R = np.stack([cube.short_rate(ccy, k) for k in range(n_s)])                                # (S, P)
    # step 0: x = 0, so the rebuilt curve must reproduce today's curve exactly
    t0_err = max(t0_err, float(np.max(np.abs(Z[0] - cube.curves[ccy].zero(taus)[:, None]))))
    parts = [pd.DataFrame({"time_step": steps, "path_id": paths, "tenor_label": lab,
                           "zero_rate": Z[:, j, :].ravel()}) for j, lab in enumerate(labels)]
    parts.append(pd.DataFrame({"time_step": steps, "path_id": paths, "tenor_label": SHORT_RATE_LABEL, "zero_rate": R.ravel()}))
    pdf = pd.concat(parts, ignore_index=True)
    pdf.insert(0, "ccy", ccy)
    pdf.insert(0, "as_of_date", as_of)
    sdf_c = spark.createDataFrame(pdf[["as_of_date", "ccy", "time_step", "path_id", "tenor_label", "zero_rate"]])
    rate_sdf = sdf_c if rate_sdf is None else rate_sdf.unionByName(sdf_c)

(rate_sdf.write
    .mode("overwrite")
    .option("replaceWhere", f"as_of_date = '{as_of}'")
    .saveAsTable(cfg.t("gold_sim_curves")))
n_rates = spark.table(cfg.t("gold_sim_curves")).filter(f"as_of_date = '{as_of}'").count()
n_expected = len(cube.hw) * n_s * n_p * (len(labels) + 1)
print(f"gold_sim_curves: {n_rates} rows ({len(cube.hw)} ccys x {n_s} steps x {n_p} paths x {len(labels) + 1} rates); "
      f"max t0 curve error {t0_err:.2e}")
ev.metric("sim_rate_rows", n_rates)
ev.metric("sim_rate_t0_max_abs_error", f"{t0_err:.2e}")
ev.check("sim rates written", n_rates == n_expected, f"{n_rates} rows (expected {n_expected})")
ev.check("rebuilt curve reprices t0", t0_err < 1e-8, f"max abs error {t0_err:.2e}")

# COMMAND ----------
# 11. Write simulated FX spot to gold_sim_fx (as_of_date, ccy, time_step, path_id, fx_rate)
# cube.fx: dict[ccy] -> np.ndarray(n_steps, n_paths), quoted as USD per 1 unit of ccy.
# USD is the base currency (constant 1.0), so it is not stored.
fx_parts, fx_t0_err = [], 0.0
for ccy, arr in cube.fx.items():
    if ccy == "USD":
        continue
    fx_t0_err = max(fx_t0_err, float(np.max(np.abs(arr[0] - fx_spot[ccy]))))
    fx_parts.append(pd.DataFrame({"ccy": ccy, "time_step": steps, "path_id": paths,
                                  "fx_rate": arr.astype(np.float64).ravel()}))
fx_pdf = pd.concat(fx_parts, ignore_index=True)
fx_pdf.insert(0, "as_of_date", as_of)
n_fx_ccys = len(fx_parts)

(spark.createDataFrame(fx_pdf[["as_of_date", "ccy", "time_step", "path_id", "fx_rate"]])
    .write
    .mode("overwrite")
    .option("replaceWhere", f"as_of_date = '{as_of}'")
    .saveAsTable(cfg.t("gold_sim_fx")))
n_fx = spark.table(cfg.t("gold_sim_fx")).filter(f"as_of_date = '{as_of}'").count()
n_fx_expected = n_fx_ccys * n_s * n_p
print(f"gold_sim_fx: {n_fx} rows ({n_fx_ccys} ccys x {n_s} steps x {n_p} paths); max t0 spot error {fx_t0_err:.2e}")
ev.metric("sim_fx_rows", n_fx)
ev.check("sim fx written", n_fx == n_fx_expected, f"{n_fx} rows (expected {n_fx_expected})")
ev.check("sim fx starts at spot", fx_t0_err < 1e-6, f"max abs error {fx_t0_err:.2e}")
ev.save()
