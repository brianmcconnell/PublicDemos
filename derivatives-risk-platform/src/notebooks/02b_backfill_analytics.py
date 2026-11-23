"""02b_backfill_analytics: build historic curves, vols and correlation from backfilled data.

Runs 02_bootstrap_curves logic for every unique FRED curve date, then 03/04 for weekly calc dates.
Written once after backfill_market completes.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets, CCYS, CORR_WINDOW_DAYS
from riskcore.curves import bootstrap_usd_treasury, curve_from_zero_points, curve_rows
from riskcore.vol import factor_levels, compute_vols
from riskcore.corr import correlation_snapshot
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("02b_backfill_analytics")

# COMMAND ----------
# 1. Historic curves for every available FRED curve date
fr = spark.sql(f"SELECT * FROM {cfg.t('bronze_fred_rates')}").toPandas()
dates = sorted(fr[fr.category == "CURVE_PAR"].obs_date.unique())
print(f"Building curves for {len(dates)} dates")

all_curve_rows = []
for d in dates:
    day = fr[fr.obs_date == d]
    usd_par = dict(zip(day[day.category == "CURVE_PAR"].tenor_label, day[day.category == "CURVE_PAR"].value / 100.0))
    if not usd_par:
        continue
    zeros = {"USD": bootstrap_usd_treasury(d, usd_par)}
    for ccy in [c for c in CCYS if c != "USD"]:
        sub = day[(day.ccy == ccy) & (day.category.isin(["CURVE_ZERO", "OVERNIGHT"]))]
        pts = dict(zip(sub.tenor_years, sub.value / 100.0))
        if pts:
            zeros[ccy] = curve_from_zero_points(d, pts)
    for ccy, zc in zeros.items():
        all_curve_rows.extend(curve_rows(d, ccy, zc, "FRED", "ql_bootstrap" if ccy == "USD" else "zero_interp",
                                          ccy not in ("USD", "EUR", "GBP")))

if all_curve_rows:
    sdf = spark.createDataFrame(pd.DataFrame(all_curve_rows))
    sdf.createOrReplaceTempView("_hc_stg")
    spark.sql(f"""
        MERGE INTO {cfg.t('gold_zero_curves')} t
        USING _hc_stg s ON t.as_of_date = s.as_of_date AND t.ccy = s.ccy AND t.tenor_label = s.tenor_label
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
print(f"Wrote {len(all_curve_rows)} historic curve rows")
ev.metric("curve rows", len(all_curve_rows))
ev.metric("curve dates", len(dates))

# COMMAND ----------
# 2. Historic vols (weekly calc dates)
# factor_levels expects long format: obs_date, ccy, tenor, zero_rate
zero_long = spark.sql(f"""
    SELECT as_of_date AS obs_date, ccy, tenor_label AS tenor, zero_rate FROM {cfg.t('gold_zero_curves')}
    WHERE tenor_label IN ('1Y','10Y') ORDER BY as_of_date
""").toPandas()

fx = spark.sql(f"SELECT obs_date, ccy, value AS usd_per_ccy FROM {cfg.t('bronze_fx_rates')}").toPandas()
fx_wide = fx.pivot_table(index="obs_date", columns="ccy", values="usd_per_ccy")
fx_wide.index = pd.to_datetime(fx_wide.index)

eq = spark.sql(f"SELECT trade_date, ticker, adj_close FROM {cfg.t('bronze_equity_prices')}").toPandas()
eq_wide = eq.pivot_table(index="trade_date", columns="ticker", values="adj_close")
eq_wide.index = pd.to_datetime(eq_wide.index)

levels, rtype = factor_levels(zero_long, fx_wide, eq_wide)
calc_dates = pd.date_range(start=levels.index.min() + pd.Timedelta(days=260), end=levels.index.max(), freq="W-FRI")
vol_wide = compute_vols(levels, rtype, calc_dates)
# Melt from wide to long for gold_hist_vol schema
vol_cols = ["vol_3m", "vol_1y", "vol_3y", "vol_10y", "vol_ewma"]
vol_df = vol_wide.rename(columns={"risk_factor": "factor_name"}).melt(
    id_vars=["calc_date", "factor_name", "return_type", "n_obs"],
    value_vars=vol_cols, var_name="window", value_name="ann_vol"
).dropna(subset=["ann_vol"])
print(f"Historic vol rows: {len(vol_df)} for {len(calc_dates)} dates")
if len(vol_df):
    sdf = spark.createDataFrame(vol_df[["calc_date", "factor_name", "return_type", "window", "ann_vol", "n_obs"]])
    sdf.createOrReplaceTempView("_hvol_stg")
    spark.sql(f"""
        MERGE INTO {cfg.t('gold_hist_vol')} t
        USING _hvol_stg s ON t.calc_date = s.calc_date AND t.factor_name = s.factor_name AND t.window = s.window
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
ev.metric("vol rows", len(vol_df))

# COMMAND ----------
# 3. Correlation on last available date
if len(levels) > CORR_WINDOW_DAYS:
    _, corr_rows = correlation_snapshot(levels, rtype, levels.index[-1], CORR_WINDOW_DAYS)
    if corr_rows:
        corr_out = pd.DataFrame(corr_rows)[["calc_date", "factor_i", "factor_j", "correlation"]]
        sdf = spark.createDataFrame(corr_out)
        sdf.createOrReplaceTempView("_hcorr_stg")
        spark.sql(f"""
            MERGE INTO {cfg.t('gold_corr_matrix')} t
            USING _hcorr_stg s ON t.calc_date = s.calc_date AND t.factor_i = s.factor_i AND t.factor_j = s.factor_j
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
    ev.metric("corr rows", len(corr_rows))

ev.check("backfill analytics done", len(all_curve_rows) > 0)
ev.save()
