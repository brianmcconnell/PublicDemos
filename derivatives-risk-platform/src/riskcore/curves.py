"""Zero-coupon curve construction.

* USD: QuantLib PiecewiseLogCubicDiscount bootstrap from Treasury CMT par yields
  (deposits <= 1Y, semi-annual par bonds beyond).
* Other currencies: inputs are already zero / spot rates (ECB AAA spot curve, BoE nominal
  zero-coupon gilts, OECD 3M/10Y proxies). They are loaded into a QuantLib ZeroCurve
  (linear in zero rate, continuous compounding) and sampled on the standard tenor grid.

Output of every builder: dict tenor_label -> continuously compounded ACT/365 zero rate.
``ZeroCurve`` is the numpy twin used by the simulation and the vectorised pricers; it uses
the same node set and interpolation as the QuantLib ZeroCurve so t0 prices agree.
"""
from __future__ import annotations

import math

import numpy as np

from .config import TENORS

EXTRAP_NODE = 50.0   # flat node beyond 30Y so linear interpolation never extrapolates


class ZeroCurve:
    """Linear-in-zero-rate curve on ACT/365 year fractions, flat at both ends."""

    def __init__(self, times, zeros):
        # nodes snapped to whole days so the QuantLib twin (date-based) has identical nodes
        t = np.round(np.asarray(times, dtype=float) * 365.0) / 365.0
        z = np.asarray(zeros, dtype=float)
        order = np.argsort(t)
        t, z = t[order], z[order]
        if t[0] > 0:
            t = np.concatenate([[0.0], t])
            z = np.concatenate([[z[0]], z])
        if t[-1] < EXTRAP_NODE:
            t = np.concatenate([t, [EXTRAP_NODE]])
            z = np.concatenate([z, [z[-1]]])
        self.times, self.zeros = t, z

    @classmethod
    def from_tenors(cls, zero_by_tenor: dict):
        items = [(TENORS[k], v) for k, v in zero_by_tenor.items() if v is not None and not math.isnan(v)]
        t, z = zip(*sorted(items))
        return cls(t, z)

    def zero(self, t):
        return np.interp(np.asarray(t, dtype=float), self.times, self.zeros)

    def df(self, t):
        t = np.asarray(t, dtype=float)
        return np.exp(-self.zero(t) * t)

    def log_df(self, t):
        t = np.asarray(t, dtype=float)
        return -self.zero(t) * t

    def fwd(self, t, h: float = 1e-4):
        """Instantaneous forward f(0,t) by central difference of ln P."""
        t = np.asarray(t, dtype=float)
        lo = np.maximum(t - h, 0.0)
        hi = t + h
        return -(self.log_df(hi) - self.log_df(lo)) / (hi - lo)

    def to_ql(self, as_of):
        """Equivalent QuantLib ZeroCurve (same nodes, linear zero, continuous, ACT/365F)."""
        import QuantLib as ql
        ref = _ql_date(as_of)
        dates = [ref + int(round(t * 365.0)) for t in self.times]
        curve = ql.ZeroCurve(dates, [float(z) for z in self.zeros], ql.Actual365Fixed(),
                             ql.NullCalendar(), ql.Linear(), ql.Continuous, ql.Annual)
        curve.enableExtrapolation()
        return curve


def _ql_date(d):
    import QuantLib as ql
    import pandas as pd
    d = pd.Timestamp(d)
    return ql.Date(d.day, d.month, d.year)


def _sample(curve) -> dict:
    import QuantLib as ql
    out = {}
    for lab, t in TENORS.items():
        out[lab] = curve.zeroRate(float(t), ql.Continuous, ql.Annual, True).rate()
    return out


def bootstrap_usd_treasury(as_of, par_yields: dict) -> dict:
    """Bootstrap a USD zero curve from Treasury CMT par yields (decimals) with QuantLib.

    par_yields: {tenor_label: yield}. <=1Y treated as money-market deposits, >1Y as
    semi-annual par bonds (price 100, coupon = par yield).
    """
    import QuantLib as ql
    ref = _ql_date(as_of)
    ql.Settings.instance().evaluationDate = ref
    cal, dc = ql.NullCalendar(), ql.Actual365Fixed()
    helpers = []
    for lab, r in sorted(par_yields.items(), key=lambda kv: TENORS[kv[0]]):
        if r is None or (isinstance(r, float) and math.isnan(r)):
            continue
        T = TENORS[lab]
        if T <= 1.0:
            months = int(round(T * 12))
            helpers.append(ql.DepositRateHelper(ql.QuoteHandle(ql.SimpleQuote(float(r))),
                                                ql.Period(months, ql.Months), 0, cal,
                                                ql.Unadjusted, False, dc))
        else:
            years = int(round(T))
            sched = ql.Schedule(ref, ref + ql.Period(years, ql.Years), ql.Period(ql.Semiannual), cal,
                                ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False)
            helpers.append(ql.FixedRateBondHelper(ql.QuoteHandle(ql.SimpleQuote(100.0)), 0, 100.0,
                                                  sched, [float(r)], dc))
    if len(helpers) < 4:
        raise ValueError(f"not enough USD par points on {as_of}: {len(helpers)}")
    curve = ql.PiecewiseLogCubicDiscount(ref, helpers, dc)
    curve.enableExtrapolation()
    return _sample(curve)


def curve_from_zero_points(as_of, points: dict) -> dict:
    """Build a QuantLib ZeroCurve from {tenor_years: zero_rate} and sample the tenor grid."""
    import QuantLib as ql
    ref = _ql_date(as_of)
    ql.Settings.instance().evaluationDate = ref
    pts = sorted((float(t), float(z)) for t, z in points.items()
                 if z is not None and not math.isnan(z))
    if not pts:
        raise ValueError(f"no zero points on {as_of}")
    if len(pts) == 1:
        pts = [pts[0], (30.0, pts[0][1])]
    times = [0.0] + [t for t, _ in pts] + [EXTRAP_NODE]
    zeros = [pts[0][1]] + [z for _, z in pts] + [pts[-1][1]]
    dates, rates, seen = [], [], set()
    for t, z in zip(times, zeros):
        d = ref + int(round(t * 365.0))
        if d.serialNumber() in seen:
            continue
        seen.add(d.serialNumber())
        dates.append(d)
        rates.append(z)
    curve = ql.ZeroCurve(dates, rates, ql.Actual365Fixed(), ql.NullCalendar(), ql.Linear(),
                         ql.Continuous, ql.Annual)
    curve.enableExtrapolation()
    return _sample(curve)


def curve_rows(as_of, ccy: str, zeros: dict, source: str, method: str, is_proxy: bool) -> list[dict]:
    """Rows for gold_zero_curves."""
    rows = []
    for lab, z in zeros.items():
        t = TENORS[lab]
        rows.append(dict(as_of_date=as_of, ccy=ccy, tenor_label=lab, tenor_years=float(t), zero_rate=float(z),
                         source=source, method=method, is_proxy=bool(is_proxy)))
    return rows
