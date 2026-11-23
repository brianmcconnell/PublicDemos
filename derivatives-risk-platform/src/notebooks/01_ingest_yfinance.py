"""01_ingest_yfinance: fetch equity and FX prices from Yahoo Finance.

Incremental from watermark. Handles GBp -> GBP conversion for London-listed stocks.
Writes to bronze_equity_prices (equities) and bronze_fx_rates (FX spot).

FX: FRED DEX series (from bronze_fred_rates, loaded by 01_ingest_fred, which must run first)
are favoured; Frankfurter (ECB reference rates) fills dates after FRED's latest observation,
and those rows are replaced by FRED once it publishes them. See riskcore/fx.py.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import numpy as np
import pandas as pd
import yfinance as yf

from riskcore.config import cfg_from_widgets, widget, ALL_TICKERS, PENCE_TICKERS, EQUITY_CCY, FX_CCYS
from riskcore.evidence import Evidence
from riskcore import fx as fxsrc

cfg = cfg_from_widgets(dbutils)
ev = Evidence("01_ingest_yfinance")

# COMMAND ----------
# Watermark
try:
    wm = spark.sql(f"SELECT last_date FROM {cfg.t('ctl_load_watermark')} WHERE source = 'yfinance'").collect()
    start = (wm[0].last_date + dt.timedelta(days=1)).strftime("%Y-%m-%d") if wm else "2014-01-01"
except Exception:
    start = "2014-01-01"
end = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
print(f"yfinance {start} -> {end}, {len(ALL_TICKERS)} tickers")

# COMMAND ----------
# Fetch equities
raw = yf.download(ALL_TICKERS, start=start, end=end, group_by="ticker", auto_adjust=False, threads=True)
rows = []
for tkr in ALL_TICKERS:
    try:
        d = raw[tkr].dropna(subset=["Close"]) if len(ALL_TICKERS) > 1 else raw.dropna(subset=["Close"])
        for idx, row in d.iterrows():
            adj = float(row["Adj Close"] if "Adj Close" in d else row["Close"])
            if tkr in PENCE_TICKERS:
                adj /= 100.0
            rows.append(dict(ticker=tkr, trade_date=idx.date(),
                             open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]),
                             close=float(row["Close"]), adj_close=adj,
                             volume=int(row.get("Volume", 0)),
                             ccy=EQUITY_CCY.get(tkr, "USD"),
                             load_ts=dt.datetime.utcnow()))
    except Exception as exc:
        print(f"WARN: {tkr}: {exc}")
print(f"Fetched {len(rows)} equity rows")
ev.metric("equity rows", len(rows))

# COMMAND ----------
# Upsert equities
if rows:
    sdf = spark.createDataFrame(pd.DataFrame(rows))
    sdf.createOrReplaceTempView("_eq_stg")
    spark.sql(f"""
        MERGE INTO {cfg.t('bronze_equity_prices')} t
        USING _eq_stg s ON t.ticker = s.ticker AND t.trade_date = s.trade_date
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"Upserted {len(rows)} equity rows")

# COMMAND ----------
# FX spot: FRED first, Frankfurter for dates FRED has not published yet.
# The window re-reads the last fx_lookback_days so Frankfurter rows are upgraded to FRED
# once FRED catches up (FRED's H.10 lags by about a week).
fx_lookback = int(widget(dbutils, "fx_lookback_days", "45"))
fx_end = dt.date.today()
last_fx = spark.sql(f"SELECT MAX(obs_date) AS d FROM {cfg.t('bronze_fx_rates')}").collect()[0].d
fx_start = fx_end - dt.timedelta(days=fx_lookback)
if last_fx is not None and last_fx < fx_start:
    fx_start = last_fx + dt.timedelta(days=1)
sids = list(fxsrc.FX_SERIES.values())
sid_list = ", ".join(f"'{s}'" for s in sids)

fred_pdf = spark.sql(f"""
    SELECT series_id, obs_date, value FROM {cfg.t('bronze_fred_rates')}
    WHERE series_id IN ({sid_list}) AND obs_date >= DATE('{fx_start}')""").toPandas()
fred_obs = {s: dict(zip(g.obs_date, g.value)) for s, g in fred_pdf.groupby("series_id")}
last_fred = {r.series_id: r.d for r in spark.sql(f"""
    SELECT series_id, MAX(obs_date) AS d FROM {cfg.t('bronze_fred_rates')}
    WHERE series_id IN ({sid_list}) GROUP BY series_id""").collect()}

# Frankfurter only needs to cover dates after the oldest of the per-series FRED cut-offs
frank_start = max(fx_start, min([last_fred.get(s, fx_start - dt.timedelta(days=1)) for s in sids])
                  + dt.timedelta(days=1))
try:
    frank = fxsrc.fetch_frankfurter(frank_start, fx_end, fxsrc.FX_SERIES.keys())
except Exception as exc:                      # FRED rows still load if Frankfurter is down
    print(f"WARN Frankfurter {frank_start} -> {fx_end} failed: {exc}")
    frank = {}

fx_rows = fxsrc.build_fx_rows(fred_obs, last_fred, frank)
# bronze_fx_rates is USD per 1 ccy for every currency; refuse to load anything that looks inverted
bad_fx = fxsrc.check_usd_per_ccy(fx_rows)
if bad_fx:
    raise ValueError(f"{len(bad_fx)} FX rows outside USD-per-ccy bounds (quote convention?), e.g. {bad_fx[:3]}")
n_fred = sum(r["source"] == fxsrc.SOURCE_FRED for r in fx_rows)
n_frank = len(fx_rows) - n_fred
print(f"FX window {fx_start} -> {fx_end}: {n_fred} FRED rows, {n_frank} Frankfurter rows "
      f"(FRED latest: { {s: str(d) for s, d in last_fred.items()} })")
ev.metric("fx window", f"{fx_start} to {fx_end}")
ev.metric("fx rows FRED", n_fred)
ev.metric("fx rows Frankfurter", n_frank)

# COMMAND ----------
# Upsert FX: insert new dates; a FRED row replaces a Frankfurter row for the same date.
if fx_rows:
    sdf = spark.createDataFrame(pd.DataFrame(fx_rows))
    sdf.createOrReplaceTempView("_fx_stg")
    spark.sql(f"""
        MERGE INTO {cfg.t('bronze_fx_rates')} t
        USING _fx_stg s ON t.series_id = s.series_id AND t.obs_date = s.obs_date
        WHEN MATCHED AND t.source = '{fxsrc.SOURCE_FRANKFURTER}' AND s.source = '{fxsrc.SOURCE_FRED}'
          THEN UPDATE SET t.value = s.value, t.source = s.source, t.load_ts = s.load_ts
        WHEN NOT MATCHED THEN INSERT *
    """)
fx_latest = spark.sql(f"""SELECT ccy, MAX(obs_date) AS d, MAX_BY(source, obs_date) AS src
                         FROM {cfg.t('bronze_fx_rates')} GROUP BY ccy""").collect()
for r in fx_latest:
    print(f"  {r.ccy}: latest {r.d} ({r.src})")
fx_stale_days = max((fx_end - r.d).days for r in fx_latest) if fx_latest else 999

# COMMAND ----------
# Watermark (equities only: the FX load is driven by its own look-back window)
all_dates = [r["trade_date"] for r in rows]
if all_dates:
    mx = max(all_dates)
    spark.sql(f"""
        MERGE INTO {cfg.t('ctl_load_watermark')} t
        USING (SELECT 'yfinance' AS source, DATE('{mx}') AS last_date, current_timestamp() AS updated_ts) s
        ON t.source = s.source
        WHEN MATCHED THEN UPDATE SET t.last_date = s.last_date, t.updated_ts = s.updated_ts
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"Watermark -> {mx}")

# COMMAND ----------
ev.check("equity data", len(rows) > 0 or start > end, f"{len(rows)} rows")
ev.check("fx up to date", fx_stale_days <= 5,
         f"oldest latest-FX date is {fx_stale_days} days old ({n_fred} FRED, {n_frank} Frankfurter rows loaded)")
ev.save()
