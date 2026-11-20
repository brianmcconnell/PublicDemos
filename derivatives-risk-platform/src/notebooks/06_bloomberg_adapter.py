# Databricks notebook source
"""06_bloomberg_adapter: synthetic Bloomberg bond/credit data (no real Bloomberg data is used).

Generates a static set of synthetic government and corporate bonds, credit spreads and bond
reference data, and writes them to bronze_bond_prices, bronze_bond_ref, bronze_credit_spreads.
This notebook is a placeholder for a real Bloomberg feed.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets, CCYS
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("06_bloomberg_adapter")
rng = np.random.default_rng(42)
as_of = dt.date.today()

# COMMAND ----------
# Synthetic bond reference
bonds = []
for i, ccy in enumerate(CCYS * 5):
    isin = f"XS{1000000 + i:07d}0"
    bonds.append(dict(isin=isin, issuer=f"Issuer_{i:03d}", ccy=ccy,
                       coupon=round(rng.uniform(0.01, 0.06), 4),
                       maturity_date=(pd.Timestamp(as_of) + pd.DateOffset(years=int(rng.integers(1, 11)))).date(),
                       issue_date=(pd.Timestamp(as_of) - pd.DateOffset(years=int(rng.integers(1, 10)))).date(),
                       rating=rng.choice(["AAA", "AA", "A", "BBB", "BB"]),
                       sector=rng.choice(["Sovereign", "Financials", "Corporate"]),
                       source="synthetic", load_ts=dt.datetime.utcnow()))
ref_df = pd.DataFrame(bonds)
print(f"Bond ref: {len(ref_df)} bonds")

# COMMAND ----------
# Synthetic prices
prices = []
for _, b in ref_df.iterrows():
    cp = round(100 + rng.normal(0, 3), 4)
    dp = round(cp + b["coupon"] * 100 * rng.uniform(0, 0.5), 4)
    ytm = round(rng.uniform(0.01, 0.08), 6)
    prices.append(dict(isin=b["isin"], trade_date=as_of, clean_price=cp, dirty_price=dp,
                       yield_to_maturity=ytm, ccy=b["ccy"], source="synthetic", load_ts=dt.datetime.utcnow()))
prices_df = pd.DataFrame(prices)

# COMMAND ----------
# Synthetic credit spreads (ICE BofA OAS style)
spreads = []
for rating in ["AAA", "AA", "A", "BBB", "BB"]:
    for tenor in ["1Y", "3Y", "5Y", "10Y"]:
        spreads.append(dict(series_id=f"SYNTH_OAS_{rating}_{tenor}", obs_date=as_of,
                             value=round(rng.uniform(0.2, 4.0), 2), rating=rating, tenor=tenor,
                             source="synthetic", load_ts=dt.datetime.utcnow()))
spreads_df = pd.DataFrame(spreads)

# COMMAND ----------
# Write
for tbl, pdf, keys in [
    ("bronze_bond_ref", ref_df, "t.isin = s.isin"),
    ("bronze_bond_prices", prices_df, "t.isin = s.isin AND t.trade_date = s.trade_date"),
    ("bronze_credit_spreads", spreads_df, "t.series_id = s.series_id AND t.obs_date = s.obs_date"),
]:
    spark.createDataFrame(pdf).createOrReplaceTempView("_stg")
    spark.sql(f"MERGE INTO {cfg.t(tbl)} t USING _stg s ON {keys} WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *")
    print(f"{tbl}: {len(pdf)} rows")

ev.metric("bonds", len(ref_df))
ev.metric("spreads", len(spreads_df))
ev.check("synthetic data written", True)
ev.save()
