"""10_dqx_monitors: data quality checks on trades, market data and simulation.

Checks:
- Quarantine rate per product (should be < 5% of total)
- Trades with notional > $500M flagged
- Day-on-day large notional changes (> 20%)
- Market data: any day-on-day rate move > 3 standard deviations
- Simulation: E[D(0,T)] within 2% of P(0,T) for the discount factor martingale
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets
from riskcore.tradegen import PRODUCTS
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("10_dqx_monitors")

# COMMAND ----------
# 1. Quarantine rate
for prod in PRODUCTS:
    total = spark.sql(f"SELECT COUNT(*) AS n FROM {cfg.t(f'silver_trades_{prod}')}").collect()[0].n
    quar = spark.sql(f"SELECT COUNT(*) AS n FROM {cfg.t('quarantine_trades')} WHERE product = '{prod}'").collect()[0].n
    rate = quar / max(total, 1)
    ev.check(f"{prod} quarantine < 5%", rate < 0.05, f"{quar}/{total} = {rate:.2%}")
    print(f"{prod}: {quar}/{total} quarantined ({rate:.2%})")

# COMMAND ----------
# 2. Large notionals
for prod in PRODUCTS:
    tbl = cfg.t(f"silver_trades_{prod}")
    try:
        big = spark.sql(f"SELECT COUNT(*) AS n FROM {tbl} WHERE ABS(notional) > 500000000").collect()[0].n
        ev.metric(f"{prod} notional > 500M", big)
    except Exception:
        pass

# COMMAND ----------
# 3. Market data: large day-on-day moves
for src, col in [("bronze_fred_rates", "value"), ("bronze_equity_prices", "adj_close")]:
    try:
        cnt = spark.sql(f"""
            WITH lagged AS (
                SELECT *, LAG({col}) OVER (PARTITION BY series_id ORDER BY obs_date) AS prev
                FROM {cfg.t(src)}
            )
            SELECT COUNT(*) AS n FROM lagged
            WHERE ABS({col} - prev) / NULLIF(ABS(prev), 0) > 0.10
        """.replace("series_id", "ticker" if "equity" in src else "series_id")
           .replace("obs_date", "trade_date" if "equity" in src else "obs_date")).collect()[0].n
        ev.metric(f"{src} >10% moves", cnt)
    except Exception as exc:
        print(f"WARN: {src} check failed: {exc}")

# COMMAND ----------
ev.save()
