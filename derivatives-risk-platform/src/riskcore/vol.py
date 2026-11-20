"""Historical volatility of market risk factors.

Risk factor naming:
  IR_{ccy}_Z{tenor}  zero rate level, returns = daily absolute change (decimal)
  IR_{ccy}_DF1Y      1Y discount factor, returns = log returns (short-rate proxy)
  IR_{ccy}_DF10Y     10Y discount factor, log returns (used with DF1Y for Hull-White alpha)
  FX_{ccy}           USD per ccy, log returns
  EQ_{ticker}        equity / index price, log returns

Volatilities (annualised, sqrt(252)) over 3M/1Y/3Y/10Y windows form the term structure;
vol_ewma uses lambda = 0.94; vol_1d is the (non-annualised) daily stdev over 1Y and is the
threshold for the DQX one-standard-deviation move check.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import EWMA_LAMBDA, TENORS, VOL_WINDOWS

ANN = np.sqrt(252.0)


def factor_levels(zero_long: pd.DataFrame | None, fx_wide: pd.DataFrame | None,
                  eq_wide: pd.DataFrame | None, ir_tenors=("3M", "1Y", "2Y", "5Y", "10Y", "30Y")):
    """Return (levels wide DataFrame indexed by date, {factor: return_type})."""
    frames, rtype = [], {}
    if zero_long is not None and len(zero_long):
        z = zero_long.pivot_table(index="obs_date", columns=["ccy", "tenor"], values="zero_rate")
        z.index = pd.to_datetime(z.index)  # align with fx/eq Timestamp indices before concat
        for ccy in sorted({c for c, _ in z.columns}):
            for ten in ir_tenors:
                if (ccy, ten) in z.columns:
                    name = f"IR_{ccy}_Z{ten}"
                    frames.append(z[(ccy, ten)].rename(name))
                    rtype[name] = "diff"
            for ten, lab in (("1Y", "DF1Y"), ("10Y", "DF10Y")):
                if (ccy, ten) in z.columns:
                    name = f"IR_{ccy}_{lab}"
                    frames.append(np.exp(-z[(ccy, ten)] * TENORS[ten]).rename(name))
                    rtype[name] = "log"
    if fx_wide is not None and len(fx_wide):
        for c in fx_wide.columns:
            frames.append(fx_wide[c].rename(f"FX_{c}"))
            rtype[f"FX_{c}"] = "log"
    if eq_wide is not None and len(eq_wide):
        for c in eq_wide.columns:
            frames.append(eq_wide[c].rename(f"EQ_{c}"))
            rtype[f"EQ_{c}"] = "log"
    if not frames:
        return pd.DataFrame(), {}
    lv = pd.concat(frames, axis=1)
    lv.index = pd.to_datetime(lv.index)
    lv = lv.groupby(lv.index).first()  # deduplicate any residual type-mismatch rows
    lv = lv.sort_index()
    bidx = pd.bdate_range(lv.index.min(), lv.index.max())
    lv = lv.reindex(bidx).ffill(limit=3)
    lv.index.name = "obs_date"
    return lv, rtype


def returns(levels: pd.DataFrame, rtype: dict) -> pd.DataFrame:
    out = {}
    for c in levels.columns:
        s = levels[c].astype(float)
        if rtype.get(c) == "log":
            s = s.where(s > 0)
            out[c] = np.log(s).diff()
        else:
            out[c] = s.diff()
    r = pd.DataFrame(out, index=levels.index)
    # a forward-filled level produces a zero return; keep (conservative) but drop leading nans
    return r.iloc[1:]


def compute_vols(levels: pd.DataFrame, rtype: dict, calc_dates=None) -> pd.DataFrame:
    """Long table of vols per (calc_date, risk_factor). calc_dates=None -> all dates."""
    r = returns(levels, rtype)
    res = {}
    for name, win in VOL_WINDOWS.items():
        res[name] = r.rolling(win, min_periods=int(win * 0.8)).std() * ANN
    res["vol_1d"] = r.rolling(252, min_periods=200).std()
    ewm_var = (r ** 2).ewm(alpha=1 - EWMA_LAMBDA, adjust=False, ignore_na=True).mean()
    res["vol_ewma"] = np.sqrt(ewm_var) * ANN
    res["n_obs"] = r.notna().rolling(2520, min_periods=1).sum()
    if calc_dates is not None:
        idx = pd.to_datetime(pd.Index(calc_dates))
        idx = idx[idx.isin(r.index)]
        res = {k: v.loc[idx] for k, v in res.items()}
    long = None
    for k, v in res.items():
        s = _stack(v).rename(k)
        long = s.to_frame() if long is None else long.join(s, how="outer")
    long = long.reset_index()
    long.columns = ["calc_date", "risk_factor"] + list(long.columns[2:])
    long = long.dropna(subset=["vol_1y", "vol_3m", "vol_ewma"], how="all")
    long["factor_type"] = long["risk_factor"].str.split("_").str[0]
    long["return_type"] = long["risk_factor"].map(rtype)
    long["tenor"] = long["risk_factor"].map(_tenor_of)
    long["calc_date"] = pd.to_datetime(long["calc_date"]).dt.date
    cols = ["calc_date", "risk_factor", "factor_type", "tenor", "return_type", "vol_1d", "vol_ewma",
            "vol_3m", "vol_1y", "vol_3y", "vol_10y", "n_obs"]
    return long[cols]


def _stack(df: pd.DataFrame) -> pd.Series:
    try:
        return df.stack(future_stack=True)
    except TypeError:          # pandas < 2.1
        return df.stack(dropna=False)


def _tenor_of(name: str):
    if name.startswith("IR_"):
        tail = name.split("_")[-1]
        return tail[1:] if tail.startswith("Z") else tail.replace("DF", "")
    return None


def vol_term_structure(row: dict, floor: float = 0.0) -> dict:
    """{tenor_years: vol} from a gold_hist_vol row, filling gaps from neighbours."""
    from .config import VOL_TERM_POINTS
    pts = {t: row.get(k) for k, t in VOL_TERM_POINTS.items()}
    vals = [v for v in pts.values() if v is not None and np.isfinite(v)]
    fallback = row.get("vol_ewma") if row.get("vol_ewma") is not None else (vals[-1] if vals else 0.2)
    filled, last = {}, None
    for t in sorted(pts):
        v = pts[t]
        if v is None or not np.isfinite(v):
            v = last if last is not None else (vals[0] if vals else fallback)
        filled[t] = max(float(v), floor)
        last = filled[t]
    return filled


def vol_at(term: dict, T):
    """Interpolate vol at maturity T from a term structure (linear in total variance)."""
    ts = np.array(sorted(term))
    vs = np.array([term[t] for t in ts])
    T = np.maximum(np.asarray(T, dtype=float), 1e-6)
    var_nodes = vs ** 2 * ts
    tv = np.interp(T, ts, var_nodes, left=np.nan, right=np.nan)
    tv = np.where(T < ts[0], vs[0] ** 2 * T, tv)
    tv = np.where(T > ts[-1], vs[-1] ** 2 * T, tv)
    return np.sqrt(tv / T)


def instantaneous_var(term: dict, t0: float, t1: float, floor: float = 0.0) -> float:
    """sigma^2(n->n+1) = [sigma^2(0,t1) t1 - sigma^2(0,t0) t0] / (t1 - t0), floored."""
    v1 = float(vol_at(term, t1)) ** 2 * t1
    v0 = float(vol_at(term, t0)) ** 2 * t0 if t0 > 0 else 0.0
    inst = (v1 - v0) / (t1 - t0)
    return max(inst, floor ** 2)
