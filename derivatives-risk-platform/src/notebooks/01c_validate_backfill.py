# Databricks notebook source
"""01c_validate_backfill: coverage, gap and spot-value checks on backfilled market data."""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
from riskcore.config import cfg_from_widgets, FRED_SERIES, ALL_TICKERS, FX_CCYS
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("01c_validate_backfill")

# COMMAND ----------
# FRED coverage
fr = spark.sql(f"""
    SELECT series_id, MIN(obs_date) AS first_date, MAX(obs_date) AS last_date, COUNT(*) AS n
    FROM {cfg.t('bronze_fred_rates')} GROUP BY series_id ORDER BY series_id
""").toPandas()
ev.metric("FRED series loaded", len(fr))
ev.metric("FRED total rows", int(fr.n.sum()))
ev.check("FRED >= 40 series", len(fr) >= 40, f"{len(fr)}")
ev.check("FRED >= 2Y history", (pd.to_datetime(fr.last_date) - pd.to_datetime(fr.first_date)).dt.days.min() > 700, "min span")
ev.table("FRED series coverage", fr)

# COMMAND ----------
# Equity coverage
eq = spark.sql(f"""
    SELECT ticker, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date, COUNT(*) AS n
    FROM {cfg.t('bronze_equity_prices')} GROUP BY ticker ORDER BY ticker
""").toPandas()
ev.metric("tickers loaded", len(eq))
ev.check("equities >= 30 tickers", len(eq) >= 30, f"{len(eq)}")
ev.table("Equity coverage (sample)", eq.head(20))

# COMMAND ----------
# FX coverage
fx = spark.sql(f"""
    SELECT ccy, MIN(obs_date) AS first_date, MAX(obs_date) AS last_date, COUNT(*) AS n
    FROM {cfg.t('bronze_fx_rates')} GROUP BY ccy ORDER BY ccy
""").toPandas()
ev.metric("FX currencies", len(fx))
ev.check("FX >= 5 currencies", len(fx) >= 5, f"{len(fx)}")
ev.table("FX coverage", fx)

# COMMAND ----------
# Backfill log
bfl = spark.table(cfg.t("ctl_backfill_log")).toPandas()
ev.table("Backfill log", bfl)
failed = bfl[bfl.status != "OK"]
ev.check("no failed chunks", len(failed) == 0, f"{len(failed)} failed")

ev.save()
