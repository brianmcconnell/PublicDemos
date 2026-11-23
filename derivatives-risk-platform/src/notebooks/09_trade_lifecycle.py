"""09_trade_lifecycle: daily trade lifecycle -> silver_trades_*, trade_lifecycle_events, gold_rate_fixings.

For every trading day (a gold_zero_curves date) after the 'trade_lifecycle' watermark, up to end_date:
  1. book that day's new trades, struck at that day's market (riskcore.lifecycle.DAILY_NEW volumes)
  2. mark trades whose maturity (FRA: settlement) date has been reached as MATURED (never deleted)
  3. reset IRS floating legs: last_reset_date / current_fixing from the market curve of the reset date
Only changed and new rows are MERGEd, so a normal day touches a few thousand rows.

First run (no watermark) or rebuild=true: rebuilds the book from scratch over the last history_years:
  * clones the existing trade tables to *_pre_lifecycle (once) so nothing is lost
  * opening book on T0 = first trading day on/after end_date - history_years: every trade booked on the
    market history before T0 (each struck on its own trade date) that is still alive on T0
  * then runs the daily lifecycle day by day from T0 to end_date and overwrites the trade tables
Downstream pricing (13, 15) only uses trade_status = 'LIVE' trades not yet matured on the valuation date.
"""
import sys, os, time, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import numpy as np
import pandas as pd
from pyspark.sql import functions as F

from riskcore.config import cfg_from_widgets, widget
from riskcore import lifecycle as lc
from riskcore.tradegen import PRODUCTS, reference_data
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
history_years = int(widget(dbutils, "history_years", str(lc.HISTORY_YEARS)))
rebuild = widget(dbutils, "rebuild", "false").lower() == "true"
end_param = widget(dbutils, "end_date", "")
seed = int(widget(dbutils, "seed", "11"))
ev = Evidence("09_trade_lifecycle")
t_start = time.time()
T = cfg.t

# COMMAND ----------
# 1. Market history (same sources and conventions as 05_mc_simulation)
zc = spark.sql(f"SELECT as_of_date, ccy, tenor_label, zero_rate FROM {T('gold_zero_curves')}").toPandas()
fx = spark.sql(f"SELECT obs_date, ccy, value FROM {T('bronze_fx_rates')}").toPandas()
eq = spark.sql(f"SELECT trade_date, ticker, adj_close FROM {T('bronze_equity_prices')}").toPandas()
mkt = lc.MarketHistory(zc, fx, eq)
ref = reference_data()           # same seed as 08 -> identical books / counterparties / netting sets
end_date = pd.Timestamp(end_param).date() if end_param else dt.date.today()
end64 = np.datetime64(end_date, "D")
print(f"Market history {mkt.dates[0]} -> {mkt.dates[-1]} ({len(mkt.dates)} trading days); end_date {end_date}")

try:
    wm = spark.sql(f"SELECT last_date FROM {T('ctl_load_watermark')} WHERE source = 'trade_lifecycle'").collect()
    watermark = wm[0].last_date if wm else None
except Exception:
    watermark = None
full = rebuild or watermark is None


def _target_schema(table, extra):
    """Existing table schema (minus lifecycle/load cols) + lifecycle columns + load_ts."""
    from pyspark.sql.types import StructType, StructField, StringType, DateType, TimestampType
    base = [f for f in spark.table(table).schema.fields if f.name not in ("trade_status", "status_date", "load_ts")]
    names = {f.name for f in base}
    for c in extra:
        if c not in names and c not in ("trade_status", "status_date", "load_ts"):
            raise ValueError(f"{table}: generator column {c} not in table schema")
    return StructType(base + [StructField("trade_status", StringType()), StructField("status_date", DateType()),
                              StructField("load_ts", TimestampType())])


def _to_spark(pdf: pd.DataFrame, schema):
    """pandas -> Spark with the target schema (all-null columns added as typed nulls)."""
    pdf = lc.to_spark_frame(pdf)
    pdf["load_ts"] = pd.Timestamp.utcnow().tz_localize(None)
    present = [f.name for f in schema.fields if f.name in pdf and pdf[f.name].notna().any()]
    sdf = spark.createDataFrame(pdf[present])
    return sdf.select([(F.col(f.name) if f.name in present else F.lit(None)).cast(f.dataType).alias(f.name)
                       for f in schema.fields])


def _write_events(ev_pdf, mode, day_from=None):
    sdf = spark.createDataFrame(ev_pdf.astype({"market_level": float, "amount": float})).select(
        F.col("event_date").cast("date"), "trade_id", "product", "event_type", F.col("effective_date").cast("date"),
        F.col("curve_date").cast("date"), F.col("market_level").cast("double"), F.col("amount").cast("double"),
        F.col("amount_ccy").cast("string"), F.lit(run_id).alias("run_id"), F.current_timestamp().alias("load_ts"))
    if mode == "overwrite":
        sdf.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(T("trade_lifecycle_events"))
    else:
        sdf.write.mode("overwrite").option("replaceWhere", f"event_date >= DATE'{day_from}'") \
           .saveAsTable(T("trade_lifecycle_events"))


run_id = str(widget(dbutils, "run_id", dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")))

# COMMAND ----------
# 2. Days to process
if full:
    T0 = mkt.dates[np.searchsorted(mkt.dates, np.datetime64(pd.Timestamp(end_date) - pd.DateOffset(years=history_years), "D"))]
    days = mkt.dates[(mkt.dates > T0) & (mkt.dates <= end64)]
    print(f"FULL REBUILD: opening book on {T0}, then {len(days)} trading days {days[0]} -> {days[-1]}")
else:
    days = mkt.dates[(mkt.dates > np.datetime64(watermark, "D")) & (mkt.dates <= end64)]
    print(f"Incremental from watermark {watermark}: {len(days)} trading days")
ev.metric("mode", "full rebuild" if full else "incremental")
ev.metric("trading days", len(days))

if not full and len(days) == 0:
    ev.check("nothing to do", True, f"no trading day after {watermark}")
    ev.save()
    dbutils.notebook.exit("no new trading days")

# COMMAND ----------
# 3. Run the lifecycle
if full:
    # keep the previous trade set (once)
    for tbl in [f"silver_trades_{p}" for p in PRODUCTS] + ["quarantine_trades"]:
        spark.sql(f"CREATE TABLE IF NOT EXISTS {T(tbl + '_pre_lifecycle')} DEEP CLONE {T(tbl)}")
    t1 = time.time()
    state = lc.opening_book(T0, mkt, ref, seed=seed)
    print(f"Opening book on {T0}: {sum(len(v) for v in state.values()):,} live trades ({time.time() - t1:.0f}s) "
          + str({p: len(v) for p, v in state.items()}))
    opening = pd.concat([lc._events(len(df), event_date=pd.Timestamp(T0).date(), trade_id=df["trade_id"].values,
                                    product=p, event_type="OPENING_BOOK",
                                    effective_date=df["trade_date"].dt.date.values,
                                    amount=df["notional"].to_numpy(float), amount_ccy=df["ccy"].values)
                         for p, df in state.items()], ignore_index=True)
    opening_resets = lc.process_day(state, T0, mkt)          # running coupons on T0
    for df in state.values():
        df["as_of_date"] = pd.Timestamp(T0)
    t1 = time.time()
    state, events, defects = lc.run_days(state, days, mkt, ref, seed=seed)
    events = pd.concat([opening, opening_resets, events], ignore_index=True)
    print(f"Daily lifecycle over {len(days)} days: {time.time() - t1:.0f}s")
else:
    state = {}
    for p in PRODUCTS:
        state[p] = lc.normalise(spark.table(T(f"silver_trades_{p}")).where("trade_status = 'LIVE'")
                                .drop("load_ts").toPandas())
    state, events, defects = lc.run_days(state, days, mkt, ref, seed=seed, log_every=0)

# COMMAND ----------
# 4. Write trades
first_day = pd.Timestamp(days[0]) if len(days) else pd.Timestamp(T0)
counts = {}
for p in PRODUCTS:
    df = state.get(p)
    if df is None or not len(df):
        continue
    tbl = T(f"silver_trades_{p}")
    schema = _target_schema(tbl, df.columns)
    if full:
        _to_spark(df, schema).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(tbl)
        counts[p] = len(df)
    else:
        chg = df[df["as_of_date"] >= first_day]
        counts[p] = len(chg)
        if len(chg):
            _to_spark(chg, schema).createOrReplaceTempView("_lc_stg")
            spark.sql(f"""MERGE INTO {tbl} t USING _lc_stg s ON t.trade_id = s.trade_id
                          WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""")
print("Rows written / merged:", counts)

# quarantine log for the defective new tickets
if len(defects):
    q = spark.createDataFrame(defects.assign(as_of_date=pd.to_datetime(defects["as_of_date"]).dt.date)[
        ["trade_id", "product", "defect", "as_of_date"]]).withColumn("load_ts", F.current_timestamp())
    q = q.select("trade_id", "product", "defect", "load_ts", F.col("as_of_date").cast("date"))
    if full:
        q.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(T("quarantine_trades"))
    else:
        q.write.mode("overwrite").option("replaceWhere", f"as_of_date >= DATE'{first_day.date()}'") \
         .saveAsTable(T("quarantine_trades"))
elif full:
    spark.sql(f"DELETE FROM {T('quarantine_trades')}")

# lifecycle events and market fixings
_write_events(events, "overwrite" if full else "replace", first_day.date())
fix_dates = mkt.dates if full else days
fixings = spark.createDataFrame(mkt.fixing_table(fix_dates))
if full:
    fixings.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(T("gold_rate_fixings"))
else:
    fixings.createOrReplaceTempView("_fix_stg")
    spark.sql(f"""MERGE INTO {T('gold_rate_fixings')} t USING _fix_stg s
                  ON t.fixing_date = s.fixing_date AND t.ccy = s.ccy AND t.tenor_months = s.tenor_months
                  WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""")

last_day = pd.Timestamp(days[-1]).date() if len(days) else pd.Timestamp(T0).date()
spark.sql(f"""MERGE INTO {T('ctl_load_watermark')} t
    USING (SELECT 'trade_lifecycle' AS source, DATE('{last_day}') AS last_date, current_timestamp() AS updated_ts) s
    ON t.source = s.source
    WHEN MATCHED THEN UPDATE SET t.last_date = s.last_date, t.updated_ts = s.updated_ts
    WHEN NOT MATCHED THEN INSERT *""")
print(f"Watermark trade_lifecycle -> {last_day}")

# COMMAND ----------
# 5. Checks
summary = spark.sql(" UNION ALL ".join(
    f"SELECT '{p}' AS product, count_if(trade_status = 'LIVE') AS live, count_if(trade_status = 'MATURED') AS matured, "
    f"count_if(trade_status = 'LIVE' AND {'settlement_date' if p == 'fra' else 'maturity_date'} <= DATE'{last_day}') AS live_past_maturity, "
    f"count_if(trade_status = 'MATURED' AND status_date > DATE'{last_day}') AS matured_in_future "
    f"FROM {T(f'silver_trades_{p}')}" for p in PRODUCTS)).toPandas()
print(summary.to_string(index=False))
n_live = int(summary.live.sum())
ev.metric("live trades", n_live)
ev.metric("matured trades", int(summary.matured.sum()))
ev.metric("events", events["event_type"].value_counts().to_dict())
ev.check("no live trade past maturity", int(summary.live_past_maturity.sum()) == 0,
         f"{int(summary.live_past_maturity.sum())} rows")
ev.check("no future maturity flags", int(summary.matured_in_future.sum()) == 0, f"{int(summary.matured_in_future.sum())} rows")
bad_fix = spark.sql(f"""SELECT count(*) AS n FROM {T('silver_trades_irs')} t
    JOIN {T('trade_lifecycle_events')} e ON e.trade_id = t.trade_id AND e.event_type = 'RESET'
         AND e.effective_date = t.last_reset_date
    WHERE t.trade_status = 'LIVE' AND abs(t.current_fixing - e.market_level) > 1e-9""").collect()[0].n
ev.check("IRS current fixing = market fixing event", bad_fix == 0, f"{bad_fix} mismatches")
ev.metric("runtime_s", round(time.time() - t_start))
print(f"Done in {time.time() - t_start:.0f}s; live {n_live:,}")
ev.save()
