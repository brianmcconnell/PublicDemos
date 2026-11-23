"""03_calc_hist_vol: compute historical volatility for IR, FX and EQ factors -> gold_hist_vol.

Reads gold_zero_curves (as discount factors), bronze_fx_rates, bronze_equity_prices.
Computes rolling vols at 3M/1Y/3Y/10Y windows plus EWMA and writes the long table.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets, CCYS, TENORS, VOL_WINDOWS, FX_CCYS, ALL_TICKERS, PENCE_TICKERS, EQUITY_CCY
from riskcore.curves import ZeroCurve
from riskcore.vol import factor_levels, returns, compute_vols
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("03_calc_hist_vol")
as_of = dt.date.today()

# COMMAND ----------
# Build zero-rate panel from gold_zero_curves
# factor_levels expects long format: obs_date, ccy, tenor, zero_rate
zero_long = spark.sql(f"""
    SELECT as_of_date AS obs_date, ccy, tenor_label AS tenor, zero_rate
    FROM {cfg.t('gold_zero_curves')}
    WHERE tenor_label IN ('1Y','10Y')
    ORDER BY as_of_date
""").toPandas()
print(f"Zero panel: {len(zero_long)} rows, dates {zero_long.obs_date.min()} to {zero_long.obs_date.max()}")

# COMMAND ----------
# FX panel
fx = spark.sql(f"SELECT obs_date, ccy, value AS usd_per_ccy FROM {cfg.t('bronze_fx_rates')}").toPandas()
fx_wide = fx.pivot_table(index="obs_date", columns="ccy", values="usd_per_ccy")
fx_wide.index = pd.to_datetime(fx_wide.index)
print(f"FX panel: {fx_wide.shape}")

# COMMAND ----------
# Equity panel
eq = spark.sql(f"SELECT trade_date, ticker, adj_close FROM {cfg.t('bronze_equity_prices')}").toPandas()
eq_wide = eq.pivot_table(index="trade_date", columns="ticker", values="adj_close")
eq_wide.index = pd.to_datetime(eq_wide.index)
print(f"Equity panel: {eq_wide.shape}")

# COMMAND ----------
# Factor levels -> returns -> vols
levels, rtype = factor_levels(zero_long, fx_wide, eq_wide)
# Use latest available date if as_of is beyond the data
calc_date = min(pd.Timestamp(as_of), levels.index.max())
calc_dates = [calc_date]
vol_wide = compute_vols(levels, rtype, calc_dates)
# Melt from wide (vol_3m, vol_1y, vol_3y, vol_10y, vol_ewma) to long (window, ann_vol)
vol_cols = ["vol_3m", "vol_1y", "vol_3y", "vol_10y", "vol_ewma"]
vol_df = vol_wide.rename(columns={"risk_factor": "factor_name"}).melt(
    id_vars=["calc_date", "factor_name", "return_type", "n_obs"],
    value_vars=vol_cols, var_name="window", value_name="ann_vol"
).dropna(subset=["ann_vol"])
print(f"Vol rows: {len(vol_df)}")
ev.metric("vol rows", len(vol_df))

# COMMAND ----------
# Write
sdf = spark.createDataFrame(vol_df[["calc_date", "factor_name", "return_type", "window", "ann_vol", "n_obs"]])
sdf.createOrReplaceTempView("_vol_stg")
spark.sql(f"""
    MERGE INTO {cfg.t('gold_hist_vol')} t
    USING _vol_stg s ON t.calc_date = s.calc_date AND t.factor_name = s.factor_name AND t.window = s.window
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")
ev.check("vol table populated", len(vol_df) > 0, f"{len(vol_df)} rows")
ev.save()
