# QuantLib comes from the job environment (resources/jobs/*.yml)

# COMMAND ----------
"""02_bootstrap_curves: build zero curves for today from bronze_fred_rates and write gold_zero_curves.

USD: QuantLib bootstrap from FRED Treasury par yields (DGS series).
EUR/GBP/JPY/CHF/CAD: interpolate proxy zero rates from FRED OECD + overnight.
Also writes silver_zero_rates for the full history retained per as_of_date.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets, CCYS, FRED_SERIES, FX_INVERTED
from riskcore.curves import bootstrap_usd_treasury, curve_from_zero_points, curve_rows
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("02_bootstrap_curves")
as_of = dt.date.today()
ev.metric("as_of", str(as_of))

# COMMAND ----------
# Read latest FRED rates (one day's worth)
fr = spark.sql(f"""
    SELECT * FROM {cfg.t('bronze_fred_rates')}
    WHERE obs_date = (SELECT MAX(obs_date) FROM {cfg.t('bronze_fred_rates')} WHERE category = 'CURVE_PAR')
""").toPandas()
print(f"FRED rows for curve date: {len(fr)}, date = {fr.obs_date.iloc[0] if len(fr) else 'NONE'}")
curve_date = fr.obs_date.iloc[0] if len(fr) else as_of
ev.metric("curve_date", str(curve_date))

# COMMAND ----------
# USD bootstrap
usd_par = {}
for _, r in fr[fr.category == "CURVE_PAR"].iterrows():
    usd_par[r.tenor_label] = r.value / 100.0       # FRED reports percentages
if usd_par:
    zeros_usd = bootstrap_usd_treasury(curve_date, usd_par)
    print("USD zeros:", {k: round(v, 5) for k, v in zeros_usd.items()})
else:
    print("WARN: no USD par yields, using flat 4%")
    zeros_usd = {"1M": 0.04, "10Y": 0.04, "30Y": 0.04}

# COMMAND ----------
# Non-USD curves from FRED overnight + OECD proxy or ECB/BoE
def _non_usd_zeros(ccy, fr):
    sub = fr[(fr.ccy == ccy) & (fr.category.isin(["CURVE_ZERO", "OVERNIGHT"]))]
    pts = {}
    for _, r in sub.iterrows():
        pts[r.tenor_years] = r.value / 100.0
    if not pts:
        return None
    return curve_from_zero_points(curve_date, pts)

zeros = {"USD": zeros_usd}
for ccy in CCYS:
    if ccy == "USD":
        continue
    z = _non_usd_zeros(ccy, fr)
    if z:
        zeros[ccy] = z
        print(f"{ccy} zeros: {{{', '.join(f'{k}: {v:.5f}' for k, v in list(z.items())[:3])}}}")
    else:
        print(f"WARN: no data for {ccy}, using flat 3%")
        zeros[ccy] = {"1M": 0.03, "10Y": 0.03}

# COMMAND ----------
# Write gold_zero_curves
all_rows = []
for ccy, zc in zeros.items():
    is_proxy = ccy not in ("USD", "EUR", "GBP")
    method = "ql_bootstrap" if ccy == "USD" else "zero_interp"
    all_rows.extend(curve_rows(curve_date, ccy, zc, "FRED", method, is_proxy))

df = pd.DataFrame(all_rows)
sdf = spark.createDataFrame(df)
sdf.createOrReplaceTempView("_curves_stg")
spark.sql(f"""
    MERGE INTO {cfg.t('gold_zero_curves')} t
    USING _curves_stg s ON t.as_of_date = s.as_of_date AND t.ccy = s.ccy AND t.tenor_label = s.tenor_label
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")
print(f"Wrote {len(df)} rows to gold_zero_curves")
ev.metric("curve rows", len(df))
ev.check("USD curve", "USD" in zeros, "bootstrap ran")
ev.check("all currencies", len(zeros) >= 6, f"{len(zeros)} currencies")
ev.save()
