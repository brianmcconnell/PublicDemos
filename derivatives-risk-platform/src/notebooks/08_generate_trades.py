"""08_generate_trades: create synthetic trades, reference data, and write to silver + dimensions.

Generates 52k trades per product (IRS, XCCY, FRA, FX fwd, Euro opt, Amer opt) with 100 defects
each. Writes clean trades to silver_trades_*, quarantine defects, and dim_* tables.
Also writes trade files to the UC volume for the Auto Loader pipeline.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets, CCYS, FX_CCYS, PENCE_TICKERS, EQUITY_CCY
from riskcore.curves import ZeroCurve
from riskcore.tradegen import reference_data, generate_all, PRODUCTS, FILE_FORMAT
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("08_generate_trades")
as_of = dt.date.today()

# COMMAND ----------
# Load curves for realistic par rates
zc = spark.sql(f"""
    SELECT ccy, tenor_label, zero_rate FROM {cfg.t('gold_zero_curves')}
    WHERE as_of_date = (SELECT MAX(as_of_date) FROM {cfg.t('gold_zero_curves')})
""").toPandas()
curves = {}
for ccy, g in zc.groupby("ccy"):
    curves[ccy] = ZeroCurve.from_tenors(dict(zip(g.tenor_label, g.zero_rate)))

# Load FX spot
fx_df = spark.sql(f"""
    WITH latest AS (SELECT ccy, MAX(obs_date) AS d FROM {cfg.t('bronze_fx_rates')} GROUP BY ccy)
    SELECT f.ccy, f.value FROM {cfg.t('bronze_fx_rates')} f JOIN latest l ON f.ccy = l.ccy AND f.obs_date = l.d
""").toPandas()
fx_spot = dict(zip(fx_df.ccy, fx_df.value))
for c in FX_CCYS:
    fx_spot.setdefault(c, 1.0)

# Load equity spot
eq_df = spark.sql(f"""
    WITH latest AS (SELECT ticker, MAX(trade_date) AS d FROM {cfg.t('bronze_equity_prices')} GROUP BY ticker)
    SELECT e.ticker, e.adj_close FROM {cfg.t('bronze_equity_prices')} e JOIN latest l ON e.ticker = l.ticker AND e.trade_date = l.d
""").toPandas()
eq_spot = dict(zip(eq_df.ticker, eq_df.adj_close))
for t in PENCE_TICKERS:
    if t in eq_spot:
        eq_spot[t] /= 100.0
print(f"Market data: {len(curves)} curves, {len(fx_spot)} FX, {len(eq_spot)} equities")

# COMMAND ----------
# Generate
ref = reference_data()
clean, dirty, defect_log = generate_all(as_of, curves, fx_spot, eq_spot, ref)
ev.metric("total clean", sum(len(d) for d in clean.values()))
ev.metric("total defects", len(defect_log))
print(f"Clean: {sum(len(d) for d in clean.values())}, Defects: {len(defect_log)}")

# COMMAND ----------
# Write dimension tables
for tbl, pdf in [("dim_book", ref["books"][["book_id","desk","business_unit"]]),
                 ("dim_counterparty", ref["counterparties"][["counterparty_id","name","rating","sector","country"]]),
                 ("dim_netting_agreement", ref["netting"]),
                 ("dim_collateral_agreement", ref["collateral"])]:
    spark.createDataFrame(pdf).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(cfg.t(tbl))
    print(f"{tbl}: {len(pdf)} rows")

# COMMAND ----------
# Write silver trades (dirty = with defects injected) + quarantine
for prod in PRODUCTS:
    d = dirty[prod].copy()
    d["load_ts"] = dt.datetime.utcnow()
    tbl = cfg.t(f"silver_trades_{prod}")
    cols = [c for c in spark.table(tbl).columns if c in d.columns]
    spark.createDataFrame(d[cols]).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(tbl)
    print(f"{tbl}: {len(d)} rows")

defect_log["load_ts"] = dt.datetime.utcnow()
defect_log["as_of_date"] = as_of
spark.createDataFrame(defect_log).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(cfg.t("quarantine_trades"))
print(f"quarantine_trades: {len(defect_log)} rows")

# COMMAND ----------
# Write trade files to volume (for Auto Loader pipeline)
vol = cfg.vol("trade_files")
for prod, fmt in FILE_FORMAT.items():
    path = f"{vol}/{prod}"
    os.makedirs(path, exist_ok=True)
    d = clean[prod]
    if fmt == "parquet":
        d.to_parquet(f"{path}/{prod}_{as_of}.parquet", index=False)
    elif fmt == "csv":
        d.to_csv(f"{path}/{prod}_{as_of}.csv", index=False)
    elif fmt == "xml":
        d.to_xml(f"{path}/{prod}_{as_of}.xml", index=False, parser="etree")
    print(f"Wrote {len(d)} rows to {path}")

# COMMAND ----------
ev.check("trades generated", sum(len(d) for d in clean.values()) >= 300000, ">=300k")
ev.check("defects injected", len(defect_log) >= 500, f"{len(defect_log)}")
ev.save()
