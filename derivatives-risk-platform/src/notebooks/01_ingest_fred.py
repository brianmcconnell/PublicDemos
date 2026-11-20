# Databricks notebook source
"""01_ingest_fred: fetch FRED series and upsert to bronze_fred_rates.

Incremental: reads the watermark, fetches only new dates, MERGE INTO to avoid duplicates.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import requests
from fredapi import Fred

from riskcore.config import cfg_from_widgets, widget, FRED_SERIES

_FRED_REST = "https://api.stlouisfed.org/fred/series/observations"

def _fetch_series(api_key, fred_client, sid, start, end):
    """Fetch a FRED series, falling back to the REST API when fredapi chokes on None values."""
    try:
        s = fred_client.get_series(sid, observation_start=str(start), observation_end=str(end)).dropna()
        return [(d.date(), float(v)) for d, v in s.items()]
    except (TypeError, ValueError):
        resp = requests.get(_FRED_REST, params=dict(
            series_id=sid, api_key=api_key, file_type="json",
            observation_start=str(start), observation_end=str(end)))
        resp.raise_for_status()
        return [(dt.date.fromisoformat(o["date"]), float(o["value"]))
                for o in resp.json().get("observations", []) if o["value"] != "."]

from riskcore.evidence import Evidence

cfg = cfg_from_widgets(dbutils)
fred_secret = widget(dbutils, "fred_secret", "serverless_stable_7s0j2u_catalog.brian_mcconnell.fred_api_key")
parts = fred_secret.split(".")
api_key = dbutils.secrets.get(catalog=parts[0], schema=parts[1], key=parts[2])
fred = Fred(api_key=api_key)
ev = Evidence("01_ingest_fred")

# COMMAND ----------
# Read watermark
try:
    wm = spark.sql(f"SELECT last_date FROM {cfg.t('ctl_load_watermark')} WHERE source = 'fred'").collect()
    start = wm[0].last_date + dt.timedelta(days=1) if wm else dt.date(2014, 1, 1)
except Exception:
    start = dt.date(2014, 1, 1)
end = dt.date.today() - dt.timedelta(days=1)
print(f"Fetching FRED {start} -> {end} ({len(FRED_SERIES)} series)")
ev.metric("date range", f"{start} to {end}")
ev.metric("n_series", len(FRED_SERIES))

# COMMAND ----------
# Fetch all series
rows = []
failed = []
for sid, (cat, ccy, tenor, tyears, proxy) in FRED_SERIES.items():
    try:
        pts = _fetch_series(api_key, fred, sid, start, end)
        for d, v in pts:
            rows.append(dict(series_id=sid, obs_date=d, value=v,
                             category=cat, ccy=ccy,
                             tenor_label=tenor or "",
                             tenor_years=float(tyears) if tyears is not None else None,
                             is_proxy=bool(proxy),
                             load_ts=dt.datetime.now(dt.timezone.utc)))
    except Exception as exc:
        failed.append(sid)
        print(f"WARN: {sid} failed: {exc}")
print(f"Fetched {len(rows)} observations ({len(failed)} failures: {failed})")
ev.metric("rows fetched", len(rows))
ev.metric("failed series", failed)

# COMMAND ----------
# Upsert
if rows:
    pdf = pd.DataFrame(rows)
    sdf = spark.createDataFrame(pdf)
    sdf.createOrReplaceTempView("_fred_stg")
    spark.sql(f"""
        MERGE INTO {cfg.t('bronze_fred_rates')} t
        USING _fred_stg s
        ON t.series_id = s.series_id AND t.obs_date = s.obs_date
        WHEN NOT MATCHED THEN INSERT *
    """)
    new_max = pdf.obs_date.max()
    spark.sql(f"""
        MERGE INTO {cfg.t('ctl_load_watermark')} t USING (SELECT 'fred' AS source, DATE('{new_max}') AS last_date, current_timestamp() AS updated_ts) s
        ON t.source = s.source
        WHEN MATCHED THEN UPDATE SET t.last_date = s.last_date, t.updated_ts = s.updated_ts
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"Upserted {len(rows)} rows, watermark -> {new_max}")
else:
    print("No new data")

# COMMAND ----------
ev.check("rows loaded", len(rows) > 0 or start > end, f"{len(rows)} rows")
ev.save()
