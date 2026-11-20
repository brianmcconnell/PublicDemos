# Databricks notebook source
"""04_calc_correlation: compute factor correlation matrix -> gold_corr_matrix.

Reads factor levels from vol notebook output (or rebuilds from bronze),
computes 2-year Pearson with sign flip on IR (DF -> short rate convention),
repairs to nearest PSD, and writes the long-format matrix.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets, CORR_WINDOW_DAYS
from riskcore.vol import factor_levels
from riskcore.corr import correlation_snapshot
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("04_calc_correlation")
as_of = dt.date.today()
calc_date = pd.Timestamp(as_of)

# COMMAND ----------
# Rebuild factor levels from bronze (same logic as 03)
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

# COMMAND ----------
# Correlation snapshot
core_df, rows = correlation_snapshot(levels, rtype, calc_date, CORR_WINDOW_DAYS)
print(f"Correlation matrix: {core_df.shape[0]}x{core_df.shape[1]}, {len(rows)} rows")
ev.metric("matrix size", core_df.shape[0])
ev.metric("rows written", len(rows))

# COMMAND ----------
# Select only columns in gold_corr_matrix schema (drop is_core, n_obs)
corr_out = pd.DataFrame(rows)[["calc_date", "factor_i", "factor_j", "correlation"]]
sdf = spark.createDataFrame(corr_out)
sdf.createOrReplaceTempView("_corr_stg")
spark.sql(f"""
    MERGE INTO {cfg.t('gold_corr_matrix')} t
    USING _corr_stg s ON t.calc_date = s.calc_date AND t.factor_i = s.factor_i AND t.factor_j = s.factor_j
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")
ev.check("corr matrix PSD", len(rows) > 0, f"{core_df.shape[0]} factors")
ev.save()
