"""FX spot rates for bronze_fx_rates: FRED first, Frankfurter as the fallback for recent dates.

FRED's H.10 noon-NY rates (DEX* series) are the preferred source, but FRED publishes them with
a lag of about a week. Frankfurter (https://frankfurter.dev, ECB reference rates, no API key)
is used only for dates AFTER the latest FRED observation of each series. When FRED later
publishes those dates, the loader's MERGE replaces the Frankfurter rows with FRED rows.

bronze_fx_rates.value is ALWAYS USD per 1 unit of ccy (config.FX_CCYS convention), which is what every
reader (03 vols, 04 correlations, 05 simulation, 09 trade lifecycle, the app) assumes. FRED quotes
DEXUSEU / DEXUSUK as USD per ccy but DEXJPUS / DEXSZUS / DEXCAUS (FX_INVERTED) as ccy per USD, so
those are inverted here; Frankfurter (base=USD, i.e. ccy per USD) is inverted for every currency.
bronze_fred_rates keeps FRED's raw quotes. check_usd_per_ccy() rejects values outside plausible USD-per-ccy
ranges, so a quote-convention slip fails the load instead of silently corrupting prices.
"""
from __future__ import annotations

import datetime as dt

from .config import FRED_SERIES, FX_INVERTED

FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
SOURCE_FRED = "FRED"
SOURCE_FRANKFURTER = "FRANKFURTER"

# ccy -> FRED series id, e.g. {"EUR": "DEXUSEU", "JPY": "DEXJPUS", ...}
FX_SERIES = {meta[1]: sid for sid, meta in FRED_SERIES.items() if meta[0] == "FX"}


def fetch_frankfurter(start: dt.date, end: dt.date, ccys, session=None, timeout: int = 30) -> dict:
    """Daily ECB reference rates from Frankfurter: {obs_date: {ccy: ccy_per_usd}}.

    Weekends and TARGET holidays have no entry. Returns {} when start > end.
    """
    if start > end:
        return {}
    if session is None:
        import requests
        session = requests
    resp = session.get(f"{FRANKFURTER_URL}/{start.isoformat()}..{end.isoformat()}",
                       params={"base": "USD", "symbols": ",".join(sorted(ccys))}, timeout=timeout)
    resp.raise_for_status()
    out = {}
    for d, rates in (resp.json().get("rates") or {}).items():
        day = dt.date.fromisoformat(d)
        if start <= day <= end:
            out[day] = {c: float(v) for c, v in rates.items() if v}
    return out


# plausible USD per 1 ccy (wide bands: catch a 1/x slip, not market moves)
USD_PER_CCY_BOUNDS = {"EUR": (0.7, 1.8), "GBP": (0.9, 2.2), "JPY": (0.003, 0.015),
                      "CHF": (0.7, 1.6), "CAD": (0.55, 1.05)}


def fred_to_usd_per_ccy(series_id: str, value: float) -> float:
    """FRED DEX quote -> USD per 1 ccy."""
    return 1.0 / value if series_id in FX_INVERTED else value


def ccy_per_usd_to_usd_per_ccy(ccy_per_usd: float) -> float:
    return 1.0 / ccy_per_usd


def check_usd_per_ccy(rows) -> list:
    """Rows (dicts with ccy, value, obs_date, source) outside USD_PER_CCY_BOUNDS."""
    bad = []
    for r in rows:
        lo, hi = USD_PER_CCY_BOUNDS.get(r["ccy"], (0.0, float("inf")))
        if not (lo <= r["value"] <= hi):
            bad.append(r)
    return bad


def build_fx_rows(fred_obs: dict, last_fred_date: dict, frankfurter: dict, load_ts=None) -> list[dict]:
    """Combine FRED and Frankfurter into bronze_fx_rates rows, favouring FRED.

    fred_obs:       {series_id: {obs_date: value}} FRED observations in the load window (raw FRED quote)
    last_fred_date: {series_id: latest FRED obs_date overall} (None / missing = FRED has none)
    frankfurter:    output of fetch_frankfurter ({obs_date: {ccy: ccy_per_usd}})
    Frankfurter is used for a series only on dates after its latest FRED observation.
    """
    load_ts = load_ts or dt.datetime.now(dt.timezone.utc)
    rows = []
    for ccy, sid in FX_SERIES.items():
        for d, v in sorted(fred_obs.get(sid, {}).items()):
            rows.append(dict(series_id=sid, obs_date=d, value=fred_to_usd_per_ccy(sid, float(v)), ccy=ccy,
                             source=SOURCE_FRED, load_ts=load_ts))
        cutoff = last_fred_date.get(sid)
        for d, rates in sorted(frankfurter.items()):
            if (cutoff is None or d > cutoff) and rates.get(ccy):
                rows.append(dict(series_id=sid, obs_date=d, value=ccy_per_usd_to_usd_per_ccy(rates[ccy]), ccy=ccy,
                                 source=SOURCE_FRANKFURTER, load_ts=load_ts))
    return rows
