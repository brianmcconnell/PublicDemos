"""Simulation time grid, weekly seed and normal variates."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

EXCEL_EPOCH = pd.Timestamp("1899-12-30")


def simulation_dates(as_of) -> list[pd.Timestamp]:
    """Grid: t0, 10 daily (business-day) steps, 4 weekly, monthly to 2Y, annual 3Y..10Y."""
    t0 = pd.Timestamp(as_of).normalize()
    dates = [t0]
    dates += list(pd.bdate_range(t0 + pd.Timedelta(days=1), periods=10))
    last_daily = dates[-1]
    dates += [last_daily + pd.Timedelta(weeks=w) for w in range(1, 5)]
    for m in range(2, 25):
        d = t0 + pd.DateOffset(months=m)
        if d > dates[-1]:
            dates.append(d)
    dates += [t0 + pd.DateOffset(years=y) for y in range(3, 11)]
    return [pd.Timestamp(d).normalize() for d in dates]


def year_fractions(dates, as_of) -> np.ndarray:
    t0 = pd.Timestamp(as_of).normalize()
    return np.array([(pd.Timestamp(d) - t0).days / 365.0 for d in dates])


def to_years(dates, as_of) -> np.ndarray:
    """Vectorised ACT/365 year fraction from as_of for an array-like of dates (NaT -> nan)."""
    d = pd.to_datetime(pd.Series(dates)).values.astype("datetime64[D]")
    t0 = np.datetime64(pd.Timestamp(as_of).date(), "D")
    out = (d - t0).astype("float64") / 365.0
    out[np.isnat(d)] = np.nan
    return out


def weekly_seed(as_of) -> int:
    """Integer date code (days since 30-Dec-1899) of the Monday of the as_of week."""
    d = pd.Timestamp(as_of).normalize()
    monday = d - pd.Timedelta(days=d.weekday())
    return int((monday - EXCEL_EPOCH).days)


def box_muller_normals(rng: np.random.Generator, n: int) -> np.ndarray:
    """Polar Box-Muller: x1,x2 ~ U[-1,1), w = x1^2+x2^2 < 1, c = sqrt(-2 ln w / w)."""
    out = np.empty(n)
    filled = 0
    while filled < n:
        m = int((n - filled) * 0.66) + 16
        x1 = rng.random(m) * 2.0 - 1.0
        x2 = rng.random(m) * 2.0 - 1.0
        w = x1 * x1 + x2 * x2
        ok = (w < 1.0) & (w > 0.0)
        x1, x2, w = x1[ok], x2[ok], w[ok]
        c = np.sqrt(-2.0 * np.log(w) / w)
        z = np.concatenate([c * x1, c * x2])
        k = min(z.size, n - filled)
        out[filled:filled + k] = z[:k]
        filled += k
    return out


def add_months(dates: np.ndarray, months) -> np.ndarray:
    """Vectorised calendar month addition (day clipped to month end), datetime64[D]."""
    d = np.asarray(dates).astype("datetime64[D]")
    ym = d.astype("datetime64[M]")
    day = (d - ym.astype("datetime64[D]")).astype(int)
    nym = ym + np.asarray(months).astype(int)
    month_len = ((nym + 1).astype("datetime64[D]") - nym.astype("datetime64[D]")).astype(int)
    return nym.astype("datetime64[D]") + np.minimum(day, month_len - 1).astype("timedelta64[D]")


def business_days(start, end) -> pd.DatetimeIndex:
    return pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))


def today() -> dt.date:
    return dt.date.today()
