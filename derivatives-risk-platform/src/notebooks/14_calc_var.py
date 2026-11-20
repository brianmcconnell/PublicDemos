# Databricks notebook source
"""14_calc_var: finalize book-level VaR from exposure partials -> gold_book_var.

Reads BOOK partial rows (from _book_parts temp view or gold_cpty_exposure),
aggregates per book using exposure.finalize_book, and writes gold_book_var.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np

from riskcore.config import cfg_from_widgets
from riskcore.exposure import finalize_book
from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
ev = Evidence("14_calc_var")
as_of = dt.date.today()

# COMMAND ----------
# Try temp view from 13, else re-read
try:
    book_parts = spark.sql("SELECT * FROM _book_parts").toPandas()
    print(f"Book partials from temp view: {len(book_parts)} rows")
except Exception:
    print("No temp view, reading from gold_cpty_exposure partials (not yet implemented)")
    book_parts = pd.DataFrame()

# COMMAND ----------
if len(book_parts):
    var_df = pd.concat([finalize_book(g) for _, g in book_parts.groupby("book_id")])
    var_df["as_of_date"] = as_of
    spark.createDataFrame(var_df).createOrReplaceTempView("_var_stg")
    spark.sql(f"""
        MERGE INTO {cfg.t('gold_book_var')} t
        USING _var_stg s ON t.as_of_date = s.as_of_date AND t.book_id = s.book_id AND t.time_step = s.time_step
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"gold_book_var: {len(var_df)} rows")
    ev.metric("var rows", len(var_df))
    ev.metric("books", var_df.book_id.nunique())
    ev.check("VaR computed", len(var_df) > 0, f"{var_df.book_id.nunique()} books")

    # Summary
    step0 = var_df[var_df.time_step == 0].sort_values("var_95", ascending=False)
    ev.table("VaR at t0 (top 10 books)", step0.head(10))
else:
    ev.check("VaR computed", False, "no book partials")

ev.save()
