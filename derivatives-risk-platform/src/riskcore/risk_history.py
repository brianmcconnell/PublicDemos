"""Daily risk history: trade MTM, sensitivities, book-hierarchy roll-ups, market snapshot and VaR, per run.

Every table is append-only history keyed by (as_of_date, run_id). A rerun with the same run_id (e.g. a job retry)
replaces that run's rows, so history never double counts. *_latest views pick the latest run for each as_of_date.

Tables (in <catalog>.<schema>):
  gold_trade_mtm_history          one row per trade per run
  gold_trade_sensitivity_history  one row per trade, sensitivity type, risk factor and tenor per run
  gold_risk_hierarchy_history     MTM and sensitivities rolled up TOTAL > BUSINESS_UNIT > DESK > BOOK
  gold_market_snapshot_history    market inputs used for pricing (curves, HW params, FX, equity spot / vol)
  gold_var_history                book VaR / ES per time step, copied from gold_book_var on each run
"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd

STAGE_SCHEMA = ("record_type string, trade_id string, sensitivity_type string, risk_class string, "
                "risk_factor string, tenor_label string, value_usd double")
PRODUCTS = ["irs", "xccy", "fra", "fxfwd", "euro_opt", "amer_opt"]

DDL = {
    "gold_trade_mtm_history": """
        as_of_date DATE, run_id STRING, run_ts TIMESTAMP, trade_id STRING, product STRING, book_id STRING,
        desk STRING, business_unit STRING, counterparty_id STRING, ccy STRING, ccy_2 STRING, underlying STRING,
        notional DOUBLE, maturity_date DATE, mtm_usd DOUBLE""",
    "gold_trade_sensitivity_history": """
        as_of_date DATE, run_id STRING, run_ts TIMESTAMP, trade_id STRING, product STRING, book_id STRING,
        desk STRING, business_unit STRING, sensitivity_type STRING, risk_class STRING, risk_factor STRING,
        tenor_label STRING, value_usd DOUBLE""",
    "gold_risk_hierarchy_history": """
        as_of_date DATE, run_id STRING, run_ts TIMESTAMP, level STRING, node_id STRING, parent_id STRING,
        business_unit STRING, desk STRING, book_id STRING, measure STRING, risk_class STRING, risk_factor STRING,
        tenor_label STRING, value_usd DOUBLE, n_trades BIGINT""",
    "gold_market_snapshot_history": """
        as_of_date DATE, run_id STRING, run_ts TIMESTAMP, risk_factor STRING, risk_class STRING, input_type STRING,
        tenor_label STRING, value DOUBLE, description STRING""",
    "gold_var_history": """
        as_of_date DATE, run_id STRING, run_ts TIMESTAMP, book_id STRING, desk STRING, business_unit STRING,
        time_step INT, n_trades INT, t0_pv DOUBLE, mean_pv DOUBLE, p5_pv DOUBLE, p95_pv DOUBLE, var_95 DOUBLE,
        expected_shortfall_95 DOUBLE""",
}
CLUSTER = {
    "gold_trade_mtm_history": "as_of_date, book_id",
    "gold_trade_sensitivity_history": "as_of_date, book_id, sensitivity_type",
    "gold_risk_hierarchy_history": "as_of_date, level, measure",
    "gold_market_snapshot_history": "as_of_date, risk_factor",
    "gold_var_history": "as_of_date, book_id",
}
COMMENTS = {
    "gold_trade_mtm_history": "Trade mark-to-market (USD) per daily run; rerun of a run_id replaces its rows",
    "gold_trade_sensitivity_history": ("Trade cash greeks (USD) per run: FX_DELTA/EQ_DELTA per +1%, FX_GAMMA/EQ_GAMMA "
                                       "change in 1% delta per +1%, EQ_VEGA per +1 vol pt, IR_DV01 parallel +1bp, "
                                       "IR_KRD +1bp at one curve node"),
    "gold_risk_hierarchy_history": "MTM and sensitivities rolled up TOTAL > BUSINESS_UNIT > DESK > BOOK per run",
    "gold_market_snapshot_history": "Market inputs used to price trades in each run, keyed by risk_factor",
    "gold_var_history": "Book VaR95 / ES95 by time step, recorded on each run",
}


def ensure_objects(spark, t) -> None:
    """Create history tables (liquid clustered) and the *_latest / drill-through views. `t` maps name -> FQN."""
    for name, cols in DDL.items():
        spark.sql(f"CREATE TABLE IF NOT EXISTS {t(name)} ({cols}) CLUSTER BY ({CLUSTER[name]}) "
                  f"COMMENT '{COMMENTS[name]}'")
    spark.sql(f"""CREATE OR REPLACE VIEW {t('v_risk_runs')} COMMENT 'Risk history runs per as_of_date' AS
        SELECT as_of_date, run_id, max(run_ts) AS run_ts, count(*) AS n_trades, sum(mtm_usd) AS total_mtm_usd
        FROM {t('gold_trade_mtm_history')} GROUP BY as_of_date, run_id""")
    for name in DDL:
        view = name.replace("gold_", "v_").replace("_history", "_latest")
        spark.sql(f"""CREATE OR REPLACE VIEW {t(view)} COMMENT 'Latest run per as_of_date of {name}' AS
            SELECT * FROM {t(name)} QUALIFY run_ts = max(run_ts) OVER (PARTITION BY as_of_date)""")
    spark.sql(f"""CREATE OR REPLACE VIEW {t('v_trade_market_inputs_latest')}
        COMMENT 'Drill-through: market inputs each trade was priced with (latest run per as_of_date)' AS
        WITH f AS (SELECT DISTINCT as_of_date, run_id, trade_id, product, book_id, risk_factor
                   FROM {t('v_trade_sensitivity_latest')})
        SELECT f.as_of_date, f.run_id, f.trade_id, f.product, f.book_id, m.risk_factor, m.risk_class,
               m.input_type, m.tenor_label, m.value, m.description
        FROM f JOIN {t('v_market_snapshot_latest')} m USING (as_of_date, run_id, risk_factor)""")


def load_trades(spark, t, as_of=None):
    """Silver trades that are live on as_of, minus quarantined ones (one Spark DataFrame, missing columns null).

    Matured trades (trade_status = 'MATURED', or maturity on/before as_of) and trades booked after as_of are
    excluded, so pricing runs only value the live book (see riskcore.lifecycle.live_filter_sql).
    """
    from .lifecycle import live_filter_sql
    trades = None
    for p in PRODUCTS:
        df = spark.table(t(f"silver_trades_{p}"))
        if as_of is not None:
            if "trade_status" not in df.columns:
                df = df.selectExpr("*", "CAST(NULL AS STRING) AS trade_status")
            df = df.where(live_filter_sql(as_of))
        trades = df if trades is None else trades.unionByName(df, allowMissingColumns=True)
    q = spark.table(t("quarantine_trades")).select("trade_id").distinct()
    return trades.join(q, "trade_id", "left_anti")


def make_pricer(mkt, src_path: str | None = None):
    """mapInPandas function: prices each partition's trades -> MTM and SENS rows (STAGE_SCHEMA).

    `mkt` is a sensitivity.MarketT0 (valuation-date market only, a few KB), so the executors do not need the
    simulation cube itself.
    """
    def price_partition(batches):
        import sys
        if src_path and src_path not in sys.path:
            sys.path.insert(0, src_path)
        import pandas as pd
        from riskcore.sensitivity import compute
        dfs = [b for b in batches if len(b)]
        if not dfs:
            return
        trades = pd.concat(dfs, ignore_index=True)
        mtm, sens = compute(trades, mkt)
        yield pd.DataFrame({"record_type": "MTM", "trade_id": mtm.trade_id, "sensitivity_type": "MTM",
                            "risk_class": None, "risk_factor": None, "tenor_label": None,
                            "value_usd": mtm.mtm_usd.astype(float)})
        if len(sens):
            out = sens.assign(record_type="SENS")
            for c in ["risk_class", "risk_factor", "tenor_label"]:
                out[c] = out[c].astype(object).where(out[c].notna(), None)
            yield out[["record_type", "trade_id", "sensitivity_type", "risk_class", "risk_factor", "tenor_label",
                       "value_usd"]].astype({"value_usd": float})
    return price_partition


def _write(df, table: str, run_id: str) -> None:
    """Replace this run's rows in a history table (idempotent on retries)."""
    df.write.mode("overwrite").option("replaceWhere", f"run_id = '{run_id}'").saveAsTable(table)


def write_run(spark, t, stage, trades, snapshot: pd.DataFrame, as_of: dt.date, run_id: str, run_ts: dt.datetime) -> dict:
    from pyspark.sql import functions as F

    keys = [F.lit(as_of).cast("date").alias("as_of_date"), F.lit(run_id).alias("run_id"),
            F.lit(run_ts).cast("timestamp").alias("run_ts")]
    books = spark.table(t("dim_book")).select("book_id", "desk", "business_unit")
    attrs = (trades.select("trade_id", "product", "book_id", "counterparty_id", "ccy", "ccy_2", "underlying",
                           F.col("notional").cast("double").alias("notional"),
                           F.to_date("maturity_date").alias("maturity_date"))
                   .join(F.broadcast(books), "book_id", "left"))

    mtm = (stage.filter("record_type = 'MTM'").select("trade_id", F.col("value_usd").alias("mtm_usd"))
                .join(attrs, "trade_id")
                .select(*keys, "trade_id", "product", "book_id", "desk", "business_unit", "counterparty_id", "ccy",
                        "ccy_2", "underlying", "notional", "maturity_date", "mtm_usd"))
    _write(mtm, t("gold_trade_mtm_history"), run_id)

    sens = (stage.filter("record_type = 'SENS'").drop("record_type")
                 .join(attrs.select("trade_id", "product", "book_id", "desk", "business_unit"), "trade_id")
                 .select(*keys, "trade_id", "product", "book_id", "desk", "business_unit", "sensitivity_type",
                         "risk_class", "risk_factor", "tenor_label", "value_usd"))
    _write(sens, t("gold_trade_sensitivity_history"), run_id)

    # hierarchy roll-ups read back the rows just written for this run
    spark.sql(f"""CREATE OR REPLACE TEMP VIEW _run_measures AS
        SELECT trade_id, business_unit, desk, book_id, 'MTM' AS measure, 'ALL' AS risk_class, 'ALL' AS risk_factor,
               'ALL' AS tenor_label, mtm_usd AS value_usd
        FROM {t('gold_trade_mtm_history')} WHERE run_id = '{run_id}'
        UNION ALL
        SELECT trade_id, business_unit, desk, book_id, sensitivity_type, risk_class, risk_factor,
               coalesce(tenor_label, 'ALL'), value_usd
        FROM {t('gold_trade_sensitivity_history')} WHERE run_id = '{run_id}'
        UNION ALL
        SELECT trade_id, business_unit, desk, book_id, sensitivity_type, risk_class, 'ALL', 'ALL', value_usd
        FROM {t('gold_trade_sensitivity_history')} WHERE run_id = '{run_id}'""")
    dims = "measure, risk_class, risk_factor, tenor_label"
    hier = spark.sql(f"""
        SELECT DATE'{as_of}' AS as_of_date, '{run_id}' AS run_id, TIMESTAMP'{run_ts:%Y-%m-%d %H:%M:%S}' AS run_ts,
               CASE WHEN grouping(business_unit) = 1 THEN 'TOTAL' WHEN grouping(desk) = 1 THEN 'BUSINESS_UNIT'
                    WHEN grouping(book_id) = 1 THEN 'DESK' ELSE 'BOOK' END AS level,
               concat_ws('/', 'TOTAL', business_unit, desk, book_id) AS node_id,
               CASE WHEN grouping(business_unit) = 1 THEN NULL
                    WHEN grouping(desk) = 1 THEN 'TOTAL'
                    WHEN grouping(book_id) = 1 THEN concat_ws('/', 'TOTAL', business_unit)
                    ELSE concat_ws('/', 'TOTAL', business_unit, desk) END AS parent_id,
               business_unit, desk, book_id, {dims}, sum(value_usd) AS value_usd, count(DISTINCT trade_id) AS n_trades
        FROM _run_measures
        GROUP BY GROUPING SETS (({dims}), (business_unit, {dims}), (business_unit, desk, {dims}),
                                (business_unit, desk, book_id, {dims}))""")
    _write(hier, t("gold_risk_hierarchy_history"), run_id)

    snap = spark.createDataFrame(snapshot.assign(value=snapshot.value.astype(float)))
    _write(snap.select(*keys, "risk_factor", "risk_class", "input_type", "tenor_label", "value", "description"),
           t("gold_market_snapshot_history"), run_id)

    var = spark.sql(f"SELECT * FROM {t('gold_book_var')} WHERE as_of_date = (SELECT max(as_of_date) FROM {t('gold_book_var')})")
    var_as_of = var.select(F.max("as_of_date")).first()[0]
    var = (var.join(F.broadcast(books), "book_id", "left")
              .select(F.col("as_of_date"), F.lit(run_id).alias("run_id"), F.lit(run_ts).cast("timestamp").alias("run_ts"),
                      "book_id", "desk", "business_unit", F.col("time_step").cast("int"), F.col("n_trades").cast("int"),
                      "t0_pv", "mean_pv", "p5_pv", "p95_pv", "var_95", "expected_shortfall_95"))
    _write(var, t("gold_var_history"), run_id)

    counts = {n: spark.table(t(n)).filter(F.col("run_id") == run_id).count() for n in DDL}
    counts["var_as_of_date"] = str(var_as_of)
    return counts
