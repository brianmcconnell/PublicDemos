"""isda_netting_pipeline: Lakeflow Spark Declarative Pipeline for ISDA netting agreement feeds.

Medallion architecture:
  Bronze  – raw JSON records ingested from the isda_feed UC Volume via Auto Loader.
            Reads recursively from both historic/ (bulk seed) and incremental/ (daily files).
  Silver  – typed and validated records with expectations:
              FAIL  : netting_set_id or counterparty_id missing (critical key)
              DROP  : effective_date or termination_date missing, or agreement already expired
              WARN  : netting_eligible not set, or agreement type unrecognised
  Gold MV – gold_netting_agreements: latest active record per netting_set_id (max last_updated_ts).
            Only agreements whose termination_date >= today are included.

The pipeline is triggered daily by the isda_netting job (resources/jobs/isda_netting_job.yml)
before the trade lifecycle and exposure runs, so gold_netting_agreements is always fresh.

Configuration (via spark.conf / pipeline settings):
  catalog  – Unity Catalog catalog (default: main)
  schema   – schema within the catalog (default: market)
"""
import datetime as dt

from pyspark import pipelines as dp
from pyspark.sql import functions as F, SparkSession

spark = SparkSession.builder.getOrCreate()
CATALOG = spark.conf.get("catalog", "main")
SCHEMA  = spark.conf.get("schema",  "market")
FEED_PATH = f"/Volumes/{CATALOG}/{SCHEMA}/isda_feed/"


# ─── Bronze ─────────────────────────────────────────────────────────────────
@dp.table(
    name="bronze_isda_netting_raw",
    comment="Raw ISDA netting agreement records from the isda_feed UC Volume (Auto Loader)."
)
def bronze_isda_netting_raw():
    """Incrementally ingest all JSON files from both historic/ and incremental/ subfolders.

    cloudFiles.inferColumnTypes=true ensures date/boolean columns are typed at ingest.
    recursiveFileLookup reads both historic/ and incremental/ in one stream.
    _metadata columns give full audit trail per record.
    """
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.inferColumnTypes", "true")
        .option("cloudFiles.schemaHints",
                "netting_set_id STRING, counterparty_id STRING, agreement_type STRING, "
                "governing_law STRING, effective_date DATE, termination_date DATE, "
                "netting_eligible BOOLEAN, close_out_netting BOOLEAN, "
                "threshold_amount DOUBLE, minimum_transfer_amount DOUBLE, "
                "independent_amount DOUBLE, csa_in_place BOOLEAN, csa_ccy STRING, "
                "mpor_days INT, rating_trigger_enabled BOOLEAN, rating_trigger_level STRING, "
                "created_ts TIMESTAMP, last_updated_ts TIMESTAMP")
        .option("recursiveFileLookup", "true")
        .load(FEED_PATH)
        .withColumn("_source_file",  F.col("_metadata.file_path"))
        .withColumn("_ingest_ts",    F.current_timestamp())
    )


# ─── Silver ─────────────────────────────────────────────────────────────────
@dp.table(
    name="silver_isda_netting",
    comment="Validated and typed ISDA netting records. "
            "Expired agreements (termination_date < today) and incomplete records are dropped."
)
# Critical: pipeline fails if either key is missing (data feed is broken)
@dp.expect_or_fail("netting_set_id present",  "netting_set_id IS NOT NULL")
@dp.expect_or_fail("counterparty_id present", "counterparty_id IS NOT NULL")
# Structural completeness: drop records that cannot be used for netting decisions
@dp.expect_all_or_drop({
    "effective_date present":   "effective_date IS NOT NULL",
    "termination_date present": "termination_date IS NOT NULL",
    "not_expired":              "termination_date >= current_date()",
})
# Soft warnings: flag data quality issues without dropping
@dp.expect_all({
    "netting_eligible set":    "netting_eligible IS NOT NULL",
    "valid_agreement_type":    "agreement_type IN ('ISDA_1992', 'ISDA_2002')",
    "positive_threshold":      "threshold_amount IS NULL OR threshold_amount >= 0",
    "positive_mta":            "minimum_transfer_amount IS NULL OR minimum_transfer_amount >= 0",
    "mpor_in_range":           "mpor_days IS NULL OR (mpor_days >= 1 AND mpor_days <= 30)",
})
def silver_isda_netting():
    return (
        spark.readStream.table("bronze_isda_netting_raw")
        # Cast any fields that might still arrive as strings from unusual files
        .withColumn("effective_date",   F.col("effective_date").cast("date"))
        .withColumn("termination_date", F.col("termination_date").cast("date"))
        .withColumn("last_updated_ts",  F.col("last_updated_ts").cast("timestamp"))
        .withColumn("created_ts",       F.col("created_ts").cast("timestamp"))
        .withColumn("netting_eligible", F.col("netting_eligible").cast("boolean"))
        # Normalise ID casing
        .withColumn("netting_set_id",   F.upper(F.trim(F.col("netting_set_id"))))
        .withColumn("counterparty_id",  F.upper(F.trim(F.col("counterparty_id"))))
        # Carry through audit columns
        .select(
            "netting_set_id", "counterparty_id", "agreement_type", "governing_law",
            "effective_date", "termination_date", "netting_eligible", "close_out_netting",
            "threshold_amount", "minimum_transfer_amount", "independent_amount",
            "csa_in_place", "csa_ccy", "mpor_days",
            "rating_trigger_enabled", "rating_trigger_level", "collateral_eligible",
            "created_ts", "last_updated_ts", "_source_file", "_ingest_ts",
        )
    )


# ─── Gold ────────────────────────────────────────────────────────────────────
@dp.materialized_view(
    name="gold_netting_agreements",
    comment="Latest active ISDA netting agreement per netting_set_id. "
            "Incremental updates from the incremental feed produce new rows in silver; "
            "this MV always reflects the most recently received record per key. "
            "Used by 13_calc_exposure.py to validate and enrich netting keys before PFE/VaR."
)
def gold_netting_agreements():
    """Deduplicate silver to one row per netting_set_id (latest last_updated_ts wins)."""
    from pyspark.sql import Window
    w = Window.partitionBy("netting_set_id").orderBy(F.col("last_updated_ts").desc())
    return (
        spark.read.table("silver_isda_netting")
        .withColumn("_rank", F.row_number().over(w))
        .filter(F.col("_rank") == 1)
        .drop("_rank", "_source_file", "_ingest_ts")
    )
