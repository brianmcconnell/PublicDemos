"""13_calc_exposure: price all trades on the SimCube -> counterparty exposure (EE / PFE95) + book VaR.

Distributed flow:
  1. trades are grouped by netting key and packed into load-balanced bins (exposure.assign_bins);
     large keys are split into chunks, each trade is assigned to exactly one (key, chunk) -> bin
  2. trades are repartitioned by bin and priced on executors with mapInPandas (exposure.run_bucket),
     emitting partial path arrays (PART_NS / PART_NA / BOOK) and SAMPLE paths -> Delta staging table
  3. partials are finalized with applyInPandas: per counterparty (EE / PFE95) and per book (VaR / ES)
Writes gold_cpty_exposure, gold_book_var, gold_trade_prices_sample.
"""
import sys, os, io, shutil, tempfile, time, datetime as dt
SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))
sys.path.insert(0, SRC)

import pandas as pd
import numpy as np
from pyspark.sql import functions as F, Window
from pyspark.sql.types import StructType, StructField, StringType, IntegerType, DoubleType, ArrayType

from riskcore.config import cfg_from_widgets, widget
from riskcore.sim import SimCube
from riskcore.exposure import assign_bins
from riskcore.tradegen import PRODUCTS
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
n_bins = int(widget(dbutils, "n_bins", "1500"))
bin_target = int(widget(dbutils, "bin_target", "250"))
price_chunk = int(widget(dbutils, "price_chunk", "50"))
max_bins = int(widget(dbutils, "max_bins", "0"))       # >0 = smoke test on the first N bins only
ev = Evidence("13_calc_exposure")
STG = cfg.t("stg_exposure_parts")

# COMMAND ----------
# 1. Ship riskcore to the executors (zip artifact; sys.path fallback inside the UDFs)
zip_base = os.path.join(tempfile.mkdtemp(), f"riskcore_{int(time.time())}")
zip_path = shutil.make_archive(zip_base, "zip", root_dir=SRC, base_dir="riskcore")
try:
    spark.addArtifacts(zip_path, pyfile=True)
    print(f"Added artifact {os.path.basename(zip_path)}")
except Exception as e:
    print(f"WARN: addArtifacts failed ({e}); relying on workspace sys.path on executors")

# COMMAND ----------
# 2. Load the latest simulation cube (<= today) and keep its bytes for the executors
cube_dir = cfg.vol("sim_cubes")
today = dt.date.today().isoformat()
cubes = sorted(f for f in os.listdir(cube_dir) if f.startswith("cube_") and f.endswith(".npz") and f[5:15] <= today)
if not cubes:
    raise FileNotFoundError(f"No simulation cube in {cube_dir}; run 05_mc_simulation first")
cube_path = f"{cube_dir}/{cubes[-1]}"
with open(cube_path, "rb") as fh:
    cube_bytes = fh.read()
cube = SimCube.load(io.BytesIO(cube_bytes))
as_of = cube.as_of.date()
print(f"Loaded {cube_path}: {cube.n_steps} steps, {cube.n_paths} paths, as_of {as_of}, {len(cube_bytes)/1e6:.1f} MB")
ev.metric("as_of", str(as_of))
ev.metric("cube_path", cube_path)

# COMMAND ----------
# 3. Live silver trades (matured and future-dated trades excluded) minus quarantined trades
from riskcore.lifecycle import live_filter_sql
trades = None
for prod in PRODUCTS:
    df = spark.table(cfg.t(f"silver_trades_{prod}"))
    if "trade_status" not in df.columns:
        df = df.selectExpr("*", "CAST(NULL AS STRING) AS trade_status")
    df = df.where(live_filter_sql(as_of))
    trades = df if trades is None else trades.unionByName(df, allowMissingColumns=True)
n_total = trades.count()
q = spark.table(cfg.t("quarantine_trades")).select("trade_id").distinct()
trades = trades.join(q, "trade_id", "left_anti")

# netting key: validate against gold_netting_agreements (ISDA netting pipeline output).
# A trade's netting_set_id is accepted only if it exists in gold_netting_agreements AND
# netting_eligible=true. Otherwise the trade is treated as a standalone 'NA' key so it
# still gets priced but without portfolio netting benefit.
try:
    gna = spark.table(cfg.t("gold_netting_agreements")).select(
        "netting_set_id",
        F.col("netting_eligible").alias("_net_eligible"),
    )
    trades = trades.join(F.broadcast(gna), "netting_set_id", "left")
except Exception as _e:
    # gold_netting_agreements not yet available (pipeline has not run) — fall back gracefully
    print(f"WARN: gold_netting_agreements not available ({_e}); all trades treated as NA netting key")
    trades = trades.withColumn("_net_eligible", F.lit(None).cast("boolean"))

is_na = (
    F.col("netting_set_id").isNull()
    | (F.col("netting_set_id") == "NA")
    | F.col("_net_eligible").isNull()          # netting set ID not in ISDA feed
    | ~F.col("_net_eligible")                  # agreement marks as not eligible
)
trades = (
    trades
    .withColumn("_is_na", is_na)
    .withColumn("key", F.when(is_na, F.concat(F.lit("NA_"), F.col("counterparty_id")))
                        .otherwise(F.col("netting_set_id")))
    .drop("_net_eligible")
)
n_netted   = trades.filter(~F.col("_is_na")).count()
n_unnetted = trades.filter(F.col("_is_na")).count()
trades = trades.drop("_is_na")
print(f"Netting: {n_netted} trades with valid ISDA netting set, {n_unnetted} in NA keys")
ev.metric("netted_trades",   n_netted)
ev.metric("unnetted_trades", n_unnetted)
key_counts = trades.groupBy("key").agg(F.count("*").alias("n")).toPandas()
n_clean = int(key_counts.n.sum())
print(f"Trades: {n_total} total, {n_clean} after quarantine, {len(key_counts)} netting keys")
ev.metric("total trades", n_total)
ev.metric("clean trades", n_clean)

# COMMAND ----------
# 4. Load-balanced bins: each trade -> one (key, chunk) -> bin
bins = assign_bins(key_counts, target=bin_target, n_bins=min(n_bins, max(4, n_clean // bin_target)))
bins_sdf = spark.createDataFrame(bins[["key", "n_chunks", "chunk", "bin"]].astype(
    {"n_chunks": "int32", "chunk": "int32", "bin": "int32"}))
w = Window.partitionBy("key").orderBy("trade_id")
kn = bins_sdf.select("key", "n_chunks").distinct()
trades = (trades.join(F.broadcast(kn), "key")
                .withColumn("chunk", ((F.row_number().over(w) - 1) % F.col("n_chunks")).cast("int"))
                .join(F.broadcast(bins_sdf.select("key", "chunk", "bin")), ["key", "chunk"])
                .withColumn("is_sample", (F.abs(F.hash("trade_id")) % 1500) == 0))
n_bins_used = int(bins.bin.nunique())
load = bins.merge(key_counts, on="key").assign(w=lambda d: d.n / d.n_chunks).groupby("bin").w.sum()
print(f"Bins: {n_bins_used}, trades per bin min/mean/max: {load.min():.0f}/{load.mean():.0f}/{load.max():.0f}")
ev.metric("bins", n_bins_used)

# COMMAND ----------
# 5. Price on executors with mapInPandas -> staging table of partial path arrays
PART_SCHEMA = StructType([
    StructField("record_type", StringType()), StructField("counterparty_id", StringType()),
    StructField("netting_set_id", StringType()), StructField("book_id", StringType()),
    StructField("trade_id", StringType()), StructField("product", StringType()),
    StructField("time_step", IntegerType()), StructField("path_id", IntegerType()),
    StructField("n_trades", IntegerType()), StructField("pv", DoubleType()),
    StructField("pv_paths", ArrayType(DoubleType())),
])
PART_COLS = [f.name for f in PART_SCHEMA.fields]
STR_COLS = ["record_type", "counterparty_id", "netting_set_id", "book_id", "trade_id", "product"]


def price_bins(batches):
    import io, sys
    import numpy as np
    import pandas as pd
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    from riskcore.sim import SimCube
    from riskcore.exposure import Accumulator
    from riskcore.pricers_vec import price_trades
    dfs = [b for b in batches if len(b)]
    if not dfs:
        return
    pdf = pd.concat(dfs, ignore_index=True)
    if pdf.empty:
        return
    cube_ = SimCube.load(io.BytesIO(cube_bytes))
    for _, g in pdf.groupby("bin"):
        g = g.reset_index(drop=True)
        acc = Accumulator(cube_.n_steps, cube_.n_paths)
        for s in range(0, len(g), price_chunk):
            part = g.iloc[s:s + price_chunk].reset_index(drop=True)
            V = np.nan_to_num(price_trades(part, cube_), nan=0.0, posinf=0.0, neginf=0.0)
            acc.add(part, V, part["is_sample"].to_numpy(bool), 20)
            acc.trade_rows.clear()          # per-trade stats are not used downstream; keep memory flat
            del V
        out = acc.output()
        del acc
        if out.empty:
            continue
        res = pd.DataFrame(index=out.index)
        for c in STR_COLS:
            res[c] = out[c].astype(object).where(out[c].notna(), None)
        res["time_step"] = out["time_step"].astype("int32")
        res["path_id"] = pd.to_numeric(out["path_id"]).fillna(-1).astype("int32")
        res["n_trades"] = pd.to_numeric(out["n_trades"]).fillna(0).astype("int32")
        res["pv"] = pd.to_numeric(out["pv"]).astype("float64")
        res["pv_paths"] = [list(v) if isinstance(v, (list, np.ndarray)) else None for v in out["pv_paths"]]
        yield res[PART_COLS]


t0 = time.time()
to_price = trades.filter(F.col("bin") < max_bins) if max_bins > 0 else trades
n_to_price = to_price.count() if max_bins > 0 else n_clean
(to_price.repartition(min(n_bins_used, max_bins) if max_bins > 0 else n_bins_used, "bin")
       .mapInPandas(price_bins, PART_SCHEMA)
       .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(STG))
parts = spark.table(STG)
secs = time.time() - t0
print(f"Priced {n_to_price} trades in {secs:.0f}s ({1000 * secs / max(n_to_price, 1):.1f} ms/trade) "
      f"-> {parts.count()} partial rows")
ev.metric("pricing seconds", round(secs))
display(parts.groupBy("record_type").count())
if max_bins > 0:
    ev.save()
    dbutils.notebook.exit(f"smoke test: {max_bins} bins, {n_to_price} trades, {secs:.0f}s")

# COMMAND ----------
# 6. Counterparty exposure (EE / PFE95) per netting set and ALL
EXP_SCHEMA = ("counterparty_id string, netting_set_id string, time_step int, n_trades int, ee double, "
              "pfe_95 double, p5 double, p25 double, p50 double, p75 double, p95 double")


def fin_cpty(pdf):
    import sys
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    from riskcore.exposure import finalize_counterparty
    out = finalize_counterparty(pdf)
    return out.astype({"time_step": "int32", "n_trades": "int32"})


exp = (parts.filter(F.col("record_type").isin("PART_NS", "PART_NA"))
            .select("record_type", "counterparty_id", "netting_set_id", "time_step", "n_trades", "pv_paths")
            .groupBy("counterparty_id").applyInPandas(fin_cpty, EXP_SCHEMA)
            .withColumn("as_of_date", F.lit(as_of).cast("date")))
exp.createOrReplaceTempView("_exp_stg")
spark.sql(f"""
    MERGE INTO {cfg.t('gold_cpty_exposure')} t
    USING _exp_stg s ON t.as_of_date = s.as_of_date AND t.counterparty_id = s.counterparty_id
        AND t.netting_set_id = s.netting_set_id AND t.time_step = s.time_step
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")
exp_out = spark.table(cfg.t("gold_cpty_exposure")).filter(F.col("as_of_date") == F.lit(as_of))
n_exp = exp_out.count()
n_cpty = exp_out.select("counterparty_id").distinct().count()
print(f"gold_cpty_exposure: {n_exp} rows, {n_cpty} counterparties")
ev.metric("exposure rows", n_exp)
ev.metric("counterparties", n_cpty)
top_pfe = (exp_out.filter(F.col("netting_set_id") == "ALL").groupBy("counterparty_id")
                  .agg(F.max("pfe_95").alias("peak_pfe_95"), F.max("ee").alias("peak_ee"),
                       F.max("n_trades").alias("n_trades"))
                  .orderBy(F.desc("peak_pfe_95")).limit(10).toPandas())
ev.table("Top 10 counterparties by peak PFE95 (USD)", top_pfe)
print(top_pfe.to_string(index=False))

# COMMAND ----------
# 7. Book VaR (mean / p5 / p95 / VaR95 / ES95) per book and time step
VAR_SCHEMA = ("book_id string, time_step int, t0_pv double, mean_pv double, p5_pv double, p95_pv double, "
              "var_95 double, expected_shortfall_95 double, n_trades int")


# Step 7a: sum path arrays per (book, step) on workers — each group is only ~1,245 rows (~10 MB)
def sum_paths(pdf):
    import numpy as np
    total = np.zeros(len(pdf.iloc[0]["pv_paths"]))  # 1000 paths
    for arr in pdf["pv_paths"]:
        total += np.asarray(arr, dtype=float)
    return pd.DataFrame({"book_id": [pdf.iloc[0]["book_id"]], "time_step": [int(pdf.iloc[0]["time_step"])],
                         "n_trades": [int(pdf["n_trades"].sum())],
                         "pv_paths": [total.tolist()]})

SUM_SCHEMA = "book_id string, time_step int, n_trades int, pv_paths array<double>"
book_summed = (parts.filter(F.col("record_type") == "BOOK")
                    .select("book_id", "time_step", "n_trades", "pv_paths")
                    .groupBy("book_id", "time_step").applyInPandas(sum_paths, SUM_SCHEMA))

# Step 7b: collect the 25×46 = 1,150 aggregated rows and compute VaR on the driver
book_pdf = book_summed.toPandas()
from riskcore.var import book_var
var_rows = []
for bid, g in book_pdf.groupby("book_id"):
    g = g.sort_values("time_step")
    X = np.stack([np.asarray(p, dtype=float) for p in g["pv_paths"]])  # (46, 1000)
    v = book_var(bid, X)
    v["n_trades"] = g.iloc[0]["n_trades"]
    var_rows.append(v)
var_df = pd.concat(var_rows, ignore_index=True)
var_df["as_of_date"] = as_of
spark.createDataFrame(var_df).createOrReplaceTempView("_var_stg")
spark.sql(f"""
    MERGE INTO {cfg.t('gold_book_var')} t
    USING _var_stg s ON t.as_of_date = s.as_of_date AND t.book_id = s.book_id AND t.time_step = s.time_step
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")
var_out = spark.table(cfg.t("gold_book_var")).filter(F.col("as_of_date") == F.lit(as_of))
n_var = var_out.count()
n_books = var_out.select("book_id").distinct().count()
print(f"gold_book_var: {n_var} rows, {n_books} books")
ev.metric("var rows", n_var)
ev.metric("books", n_books)
var_1d = var_out.filter(F.col("time_step") == 1).orderBy(F.desc("var_95")).limit(10).toPandas()
ev.table("1-day VaR95 (top 10 books, USD)", var_1d.drop(columns=["as_of_date"]))
print(var_1d.to_string(index=False))

# COMMAND ----------
# 7b. Portfolio paths for the app's what-if tab (riskcore.whatif): book value paths, per-counterparty netting-key
#     paths and reference data in sim_cubes/portfolio_<as_of>/. manifest.json is written last and marks it complete.
import json
from riskcore import whatif

pdir = f"{cube_dir}/{whatif.PORTFOLIO_PREFIX}{as_of}"
manifest_path = f"{pdir}/manifest.json"
os.makedirs(f"{pdir}/cpty", exist_ok=True)
if os.path.exists(manifest_path):
    os.remove(manifest_path)
for f in os.listdir(f"{pdir}/cpty"):                 # counterparties from an earlier run of the same day
    os.remove(f"{pdir}/cpty/{f}")
with open(f"{pdir}/books.npz", "wb") as fh:
    fh.write(whatif.books_npz(book_pdf))


def save_cpty(pdf):
    import sys
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    from riskcore.whatif import cpty_npz
    c = pdf["counterparty_id"].iloc[0]
    blob = cpty_npz(pdf)
    with open(f"{pdir}/cpty/{c}.npz", "wb") as fh:
        fh.write(blob)
    return pd.DataFrame({"counterparty_id": [c], "n_bytes": [len(blob)]})


t_wi = time.time()
saved = (parts.filter(F.col("record_type").isin("PART_NS", "PART_NA"))
              .select("counterparty_id", "netting_set_id", "time_step", "n_trades", "pv_paths")
              .groupBy("counterparty_id").applyInPandas(save_cpty, "counterparty_id string, n_bytes long")
              .toPandas())
ref = whatif.refdata(spark.table(cfg.t("dim_book")).toPandas(), spark.table(cfg.t("dim_counterparty")).toPandas(),
                     spark.table(cfg.t("dim_netting_agreement")).toPandas(),
                     spark.table(cfg.t("dim_collateral_agreement")).toPandas(), cube)
with open(f"{pdir}/refdata.json", "w") as fh:
    json.dump(ref, fh)
manifest = dict(as_of=str(as_of), cube_file=os.path.basename(cube_path), n_steps=int(cube.n_steps),
                n_paths=int(cube.n_paths), n_books=int(book_pdf["book_id"].nunique()),
                n_counterparties=int(len(saved)), n_trades=int(n_clean),
                written_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
with open(manifest_path, "w") as fh:
    json.dump(manifest, fh)
# keep the three newest portfolio directories
old = sorted(d for d in os.listdir(cube_dir) if d.startswith(whatif.PORTFOLIO_PREFIX))[:-3]
for d in old:
    shutil.rmtree(f"{cube_dir}/{d}", ignore_errors=True)
n_files = len([f for f in os.listdir(f"{pdir}/cpty") if f.endswith(".npz")])
print(f"what-if portfolio {pdir}: {manifest['n_books']} books, {n_files} counterparty files "
      f"({saved.n_bytes.sum() / 1e6:.0f} MB) in {time.time() - t_wi:.0f}s; removed {len(old)} old directories")
ev.metric("what-if portfolio dir", pdir)
ev.check("what-if counterparty files", n_files == n_cpty, f"{n_files} files, {n_cpty} counterparties")

# COMMAND ----------
# 8. Sample trade price paths (replace today's partition only)
samples = (parts.filter(F.col("record_type") == "SAMPLE")
                .select(F.lit(as_of).cast("date").alias("as_of_date"), "trade_id", "product", "counterparty_id",
                        "time_step", "path_id", "pv"))
(samples.write.mode("overwrite").option("replaceWhere", f"as_of_date = '{as_of}'")
        .saveAsTable(cfg.t("gold_trade_prices_sample")))
n_samp = spark.table(cfg.t("gold_trade_prices_sample")).filter(F.col("as_of_date") == F.lit(as_of)).count()
print(f"gold_trade_prices_sample: {n_samp} rows")
ev.metric("sample rows", n_samp)

# COMMAND ----------
ev.check("all clean trades binned", trades.count() == n_clean, f"{n_clean} trades")
ev.check("exposure computed", n_exp > 0, f"{n_cpty} counterparties")
ev.check("VaR computed", n_var > 0, f"{n_books} books")
ev.check("steps per cpty", n_exp % cube.n_steps == 0, f"{cube.n_steps} steps")
ev.save()
