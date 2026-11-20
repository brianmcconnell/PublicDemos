# Databricks notebook source
"""01b_backfill_market: chunked historic backfill of FRED + yfinance data.

Default 10Y window (min 2Y), 12-month chunks. Each chunk is logged to ctl_backfill_log;
already-completed chunks are skipped (idempotent). Sets the watermark when done so daily
incremental picks up from the next business day.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np
import requests
from dateutil.relativedelta import relativedelta
from fredapi import Fred
import yfinance as yf

from riskcore.config import cfg_from_widgets, widget, FRED_SERIES, ALL_TICKERS, PENCE_TICKERS, EQUITY_CCY, FX_CCYS

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
ev = Evidence("01b_backfill_market")

BACKFILL_YEARS = int(widget(dbutils, "backfill_years", "10"))
CHUNK_MONTHS = int(widget(dbutils, "chunk_months", "12"))
end = dt.date.today() - dt.timedelta(days=1)
start = end - relativedelta(years=BACKFILL_YEARS)
ev.metric("backfill range", f"{start} to {end}")
ev.metric("chunk months", CHUNK_MONTHS)

# COMMAND ----------
def chunks(start, end, months):
    """Yield (chunk_start, chunk_end) tuples."""
    cur = start
    while cur <= end:
        ce = min(cur + relativedelta(months=months) - dt.timedelta(days=1), end)
        yield cur, ce
        cur = ce + dt.timedelta(days=1)

def chunk_done(source, cs, ce):
    r = spark.sql(f"""
        SELECT status FROM {cfg.t('ctl_backfill_log')}
        WHERE source = '{source}' AND chunk_start = '{cs}' AND chunk_end = '{ce}' AND status = 'OK'
    """).collect()
    return len(r) > 0

def log_chunk(source, cs, ce, status, n_rows, started):
    spark.sql(f"""
        INSERT INTO {cfg.t('ctl_backfill_log')}
        VALUES ('{source}', '{cs}', '{ce}', '{status}', {n_rows}, '{started}', current_timestamp())
    """)

# COMMAND ----------
# FRED backfill
for cs, ce in chunks(start, end, CHUNK_MONTHS):
    if chunk_done("fred", cs, ce):
        print(f"FRED {cs} -> {ce}: already done")
        continue
    t0 = dt.datetime.utcnow()
    rows = []
    for sid, (cat, ccy, tenor, tyears, proxy) in FRED_SERIES.items():
        try:
            pts = _fetch_series(api_key, fred, sid, str(cs), str(ce))
            for d, v in pts:
                rows.append(dict(series_id=sid, obs_date=d, value=v,
                                 category=cat, ccy=ccy,
                                 tenor_label=tenor or "",
                                 tenor_years=float(tyears) if tyears is not None else None,
                                 is_proxy=bool(proxy),
                                 load_ts=dt.datetime.now(dt.timezone.utc)))
        except Exception:
            pass
    if rows:
        sdf = spark.createDataFrame(pd.DataFrame(rows))
        sdf.createOrReplaceTempView("_bf_stg")
        spark.sql(f"""
            MERGE INTO {cfg.t('bronze_fred_rates')} t
            USING _bf_stg s ON t.series_id = s.series_id AND t.obs_date = s.obs_date
            WHEN NOT MATCHED THEN INSERT *
        """)
    log_chunk("fred", cs, ce, "OK", len(rows), t0)
    print(f"FRED {cs} -> {ce}: {len(rows)} rows")

# COMMAND ----------
# yfinance backfill (equities + FX)
for cs, ce in chunks(start, end, CHUNK_MONTHS):
    if chunk_done("yfinance", cs, ce):
        print(f"yfinance {cs} -> {ce}: already done")
        continue
    t0 = dt.datetime.utcnow()
    raw = yf.download(ALL_TICKERS, start=str(cs), end=str(ce + dt.timedelta(days=1)),
                      group_by="ticker", auto_adjust=False, threads=True)
    rows = []
    for tkr in ALL_TICKERS:
        try:
            d = raw[tkr].dropna(subset=["Close"]) if len(ALL_TICKERS) > 1 else raw.dropna(subset=["Close"])
            for idx, row in d.iterrows():
                adj = float(row.get("Adj Close", row["Close"]))
                if tkr in PENCE_TICKERS:
                    adj /= 100.0
                rows.append(dict(ticker=tkr, trade_date=idx.date(),
                                 open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]),
                                 close=float(row["Close"]), adj_close=adj,
                                 volume=int(row.get("Volume", 0)), ccy=EQUITY_CCY.get(tkr, "USD"),
                                 load_ts=dt.datetime.utcnow()))
        except Exception:
            pass
    if rows:
        sdf = spark.createDataFrame(pd.DataFrame(rows))
        sdf.createOrReplaceTempView("_bf_eq")
        spark.sql(f"""
            MERGE INTO {cfg.t('bronze_equity_prices')} t
            USING _bf_eq s ON t.ticker = s.ticker AND t.trade_date = s.trade_date
            WHEN NOT MATCHED THEN INSERT *
        """)
    # FX
    fx_rows = []
    for ccy in FX_CCYS:
        try:
            d = yf.download(f"{ccy}USD=X", start=str(cs), end=str(ce + dt.timedelta(days=1)), auto_adjust=False)
            for idx, row in d.dropna(subset=["Close"]).iterrows():
                fx_rows.append(dict(series_id=f"{ccy}USD=X", obs_date=idx.date(), value=float(row["Close"]),
                                    ccy=ccy, source="yahoo", load_ts=dt.datetime.utcnow()))
        except Exception:
            pass
    if fx_rows:
        sdf = spark.createDataFrame(pd.DataFrame(fx_rows))
        sdf.createOrReplaceTempView("_bf_fx")
        spark.sql(f"""
            MERGE INTO {cfg.t('bronze_fx_rates')} t
            USING _bf_fx s ON t.series_id = s.series_id AND t.obs_date = s.obs_date
            WHEN NOT MATCHED THEN INSERT *
        """)
    log_chunk("yfinance", cs, ce, "OK", len(rows) + len(fx_rows), t0)
    print(f"yfinance {cs} -> {ce}: {len(rows)} eq + {len(fx_rows)} fx")

# COMMAND ----------
# Set watermark
for src in ["fred", "yfinance"]:
    spark.sql(f"""
        MERGE INTO {cfg.t('ctl_load_watermark')} t
        USING (SELECT '{src}' AS source, DATE('{end}') AS last_date, current_timestamp() AS updated_ts) s
        ON t.source = s.source
        WHEN MATCHED THEN UPDATE SET t.last_date = s.last_date, t.updated_ts = s.updated_ts
        WHEN NOT MATCHED THEN INSERT *
    """)
print(f"Watermarks set to {end}")
ev.check("backfill complete", True, f"{start} to {end}")
ev.save()
