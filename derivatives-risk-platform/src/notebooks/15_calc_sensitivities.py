# Databricks notebook source
"""15_calc_sensitivities: daily trade MTM and greeks, rolled up the book hierarchy, with run history.

Prices every clean trade at the valuation date of the latest simulation cube with bump-and-reprice
(riskcore.sensitivity), distributed with mapInPandas, then appends this run to the history tables in
riskcore.risk_history (trade MTM, trade sensitivities, TOTAL > BUSINESS_UNIT > DESK > BOOK roll-ups, the market
snapshot used for pricing, and book VaR from gold_book_var).

Parameters: catalog, schema, run_id (the job passes {{job.run_id}}; blank = manual run), n_parts.
Runs as a notebook (widgets) or as a Python script task (--catalog ... --run_id ... arguments).
Re-running with the same run_id replaces that run's rows.
"""
import os, sys, io, shutil, tempfile, time, argparse, datetime as dt
try:
    _here = os.path.dirname(os.path.abspath(__file__))
except NameError:                                   # notebook: __file__ is not defined
    _here = os.path.dirname(os.path.abspath("__file__"))
SRC = os.path.abspath(os.path.join(_here, ".."))
sys.path.insert(0, SRC)

from pyspark.sql import SparkSession, functions as F

from riskcore.config import Cfg, cfg_from_widgets, widget
from riskcore.sim import SimCube
from riskcore.sensitivity import MarketT0
from riskcore import risk_history as rh
from riskcore.evidence import Evidence

try:
    spark
except NameError:
    spark = SparkSession.builder.getOrCreate()

_ap = argparse.ArgumentParser()
for _k in ("--catalog", "--schema", "--run_id", "--n_parts"):
    _ap.add_argument(_k)
_args, _ = _ap.parse_known_args()
if _args.catalog:                                   # Python script task
    cfg = Cfg(catalog=_args.catalog, schema=_args.schema or Cfg.schema)
    _run_id, _n_parts = _args.run_id or "", _args.n_parts or "1200"
else:                                               # notebook
    cfg = cfg_from_widgets(dbutils)
    _run_id, _n_parts = widget(dbutils, "run_id", ""), widget(dbutils, "n_parts", "1200")
run_id = _run_id.strip() or f"manual-{dt.datetime.utcnow():%Y%m%dT%H%M%S}"
n_parts = int(_n_parts)                             # ~260 trades per task keeps worker memory small
run_ts = dt.datetime.utcnow().replace(microsecond=0)
ev = Evidence("15_calc_sensitivities")
ev.metric("run_id", run_id)
print(f"run_id {run_id}, run_ts {run_ts}")

# COMMAND ----------
# 1. History tables and views
rh.ensure_objects(spark, cfg.t)

# 2. Ship riskcore to the executors
zip_base = os.path.join(tempfile.mkdtemp(), f"riskcore_{int(time.time())}")
zip_path = shutil.make_archive(zip_base, "zip", root_dir=SRC, base_dir="riskcore")
try:
    spark.addArtifacts(zip_path, pyfile=True)
except Exception as e:
    print(f"WARN: addArtifacts failed ({e}); relying on workspace sys.path on executors")

# 3. Latest simulation cube = valuation date and market data
cube_dir = cfg.vol("sim_cubes")
cubes = sorted(f for f in os.listdir(cube_dir) if f.startswith("cube_") and f.endswith(".npz"))
if not cubes:
    raise FileNotFoundError(f"No simulation cube in {cube_dir}; run 05_mc_simulation first")
with open(f"{cube_dir}/{cubes[-1]}", "rb") as fh:
    cube_bytes = fh.read()
mkt = MarketT0(SimCube.load(io.BytesIO(cube_bytes)))
as_of = mkt.as_of.date()
print(f"Valuation date {as_of} from {cubes[-1]}")
ev.metric("as_of", str(as_of))

# COMMAND ----------
# 4. Price all clean trades (MTM + greeks) on the executors -> staging table
trades = rh.load_trades(spark, cfg.t, as_of)      # live trades only: matured / future-dated excluded
n_trades = trades.count()
t0 = time.time()
(trades.repartition(n_parts, "trade_id")
       .mapInPandas(rh.make_pricer(mkt, SRC), rh.STAGE_SCHEMA)
       .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(cfg.t("stg_sensitivities")))
stage = spark.table(cfg.t("stg_sensitivities"))
secs = time.time() - t0
print(f"Priced {n_trades} trades in {secs:.0f}s")
ev.metric("trades", n_trades)
ev.metric("pricing seconds", round(secs))

# COMMAND ----------
# 5. Append this run to the history tables
counts = rh.write_run(spark, cfg.t, stage, trades, mkt.snapshot(), as_of, run_id, run_ts)
for k, v in counts.items():
    print(f"{k}: {v}")
    ev.metric(k, v)

# COMMAND ----------
# 6. Checks
H = spark.table(cfg.t("gold_risk_hierarchy_history")).filter(F.col("run_id") == run_id)
trade_mtm = spark.table(cfg.t("gold_trade_mtm_history")).filter(F.col("run_id") == run_id).agg(F.sum("mtm_usd")).first()[0]
total_mtm = H.filter("level = 'TOTAL' AND measure = 'MTM'").first().value_usd
book_mtm = H.filter("level = 'BOOK' AND measure = 'MTM'").agg(F.sum("value_usd")).first()[0]
sens_tot = H.filter("level = 'TOTAL' AND risk_factor = 'ALL'").select("measure", "value_usd").toPandas().set_index("measure").value_usd
# per trade and currency: |sum of key rates - parallel DV01|, relative to gross (unsigned) DV01
_g = spark.sql(f"""SELECT sum(abs(krd - dv01)) AS gap, sum(abs(dv01)) AS gross FROM (
    SELECT trade_id, risk_factor,
           sum(CASE WHEN sensitivity_type = 'IR_DV01' THEN value_usd END) AS dv01,
           coalesce(sum(CASE WHEN sensitivity_type = 'IR_KRD' THEN value_usd END), 0) AS krd
    FROM {cfg.t('gold_trade_sensitivity_history')}
    WHERE run_id = '{run_id}' AND sensitivity_type IN ('IR_DV01', 'IR_KRD')
    GROUP BY trade_id, risk_factor)""").first()
krd_vs_dv01 = _g.gap / max(_g.gross, 1)
ev.metric("gross |DV01| (USD)", round(_g.gross))
ev.metric("net key rates - net DV01 (USD)", round(sens_tot.get("IR_KRD", 0) - sens_tot.get("IR_DV01", 0)))
print("Total sensitivities (USD):"); print(sens_tot.round(0).to_string())
ev.table("Total sensitivities (USD)", sens_tot.round(0).reset_index())
ev.check("every trade has an MTM", counts["gold_trade_mtm_history"] == n_trades, f"{counts['gold_trade_mtm_history']} of {n_trades}")
ev.check("hierarchy TOTAL = sum of trades", abs(total_mtm - trade_mtm) <= 1e-6 * max(abs(trade_mtm), 1), f"{total_mtm:,.0f}")
ev.check("hierarchy books = TOTAL", abs(book_mtm - total_mtm) <= 1e-6 * max(abs(total_mtm), 1), f"{book_mtm:,.0f}")
ev.check("sum of key rates ~ parallel DV01 (per trade, vs gross DV01)", krd_vs_dv01 < 1e-3,
         f"rel diff {krd_vs_dv01:.1e}")
ev.check("VaR history recorded", counts["gold_var_history"] > 0, f"{counts['gold_var_history']} rows, as_of {counts['var_as_of_date']}")
ev.save()
if not ev.all_passed:
    raise RuntimeError("15_calc_sensitivities: checks failed - see the evidence output above")
