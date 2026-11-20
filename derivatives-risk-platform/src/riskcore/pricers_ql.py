"""QuantLib pricers: today's valuation, cross-check of the vectorised grid, app trade pricer.

All prices are returned in USD. Market inputs come from a ``QLMarket`` built from the gold tables
(zero curves, FX spot, equity spot, equity vol term structure). Conventions match pricers_vec:
ACT/365F, NullCalendar/unadjusted schedules generated forward from the start date, cash flows on
the valuation date excluded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .curves import ZeroCurve
from .vol import vol_at


def _ql():
    import QuantLib as ql
    return ql


def qdate(d):
    ql = _ql()
    d = pd.Timestamp(d)
    return ql.Date(d.day, d.month, d.year)


@dataclass
class QLMarket:
    as_of: pd.Timestamp
    zeros: dict                         # ccy -> {tenor_label: zero}
    fx_spot: dict                       # ccy -> USD per ccy
    eq_spot: dict = field(default_factory=dict)
    eq_vol: dict = field(default_factory=dict)      # ticker -> {tenor_years: vol}
    eq_ccy: dict = field(default_factory=dict)

    def __post_init__(self):
        ql = _ql()
        self.ref = qdate(self.as_of)
        ql.Settings.instance().evaluationDate = self.ref
        self._curves = {c: ZeroCurve.from_tenors(z).to_ql(self.as_of) for c, z in self.zeros.items()}
        self._handles = {c: ql.YieldTermStructureHandle(cv) for c, cv in self._curves.items()}

    def curve(self, ccy):
        return self._curves[ccy]

    def handle(self, ccy):
        return self._handles[ccy]

    def fx(self, ccy) -> float:
        return 1.0 if ccy == "USD" else float(self.fx_spot[ccy])

    def df(self, ccy, d) -> float:
        d = qdate(d)
        return self._curves[ccy].discount(d) if d > self.ref else 0.0


def _sched(start, end, months):
    ql = _ql()
    return ql.Schedule(qdate(start), qdate(end), ql.Period(int(months), ql.Months), ql.NullCalendar(),
                       ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Forward, False)


def _months_between(a, b) -> int:
    a, b = pd.Timestamp(a), pd.Timestamp(b)
    return (b.year - a.year) * 12 + (b.month - a.month)


def _float_swap(mkt: QLMarket, ccy, payer: bool, N, K, start, end, freq, last_reset=None, fixing=None):
    ql = _ql()
    ql.Settings.instance().evaluationDate = mkt.ref
    h = mkt.handle(ccy)
    dc = ql.Actual365Fixed()
    sched = _sched(start, end, freq)
    index = ql.IborIndex(f"SIM{ccy}{int(freq)}M", ql.Period(int(freq), ql.Months), 0, ql.USDCurrency(),
                         ql.NullCalendar(), ql.Unadjusted, False, dc, h)
    if last_reset is not None and pd.notna(last_reset) and fixing is not None and pd.notna(fixing):
        index.addFixing(qdate(last_reset), float(fixing), True)
    typ = ql.VanillaSwap.Payer if payer else ql.VanillaSwap.Receiver
    swap = ql.VanillaSwap(typ, float(N), sched, float(K), dc, sched, index, 0.0, dc)
    swap.setPricingEngine(ql.DiscountingSwapEngine(h))
    npv = swap.NPV()
    index.clearFixings()
    return npv


def price_irs(r, mkt: QLMarket) -> float:
    v = _float_swap(mkt, r["ccy"], r["direction"] == "PAY", r["notional"], r["fixed_rate"], r["start_date"],
                    r["maturity_date"], r["pay_freq_months"], r.get("last_reset_date"), r.get("current_fixing"))
    return v * mkt.fx(r["ccy"])


def price_fra(r, mkt: QLMarket) -> float:
    if pd.Timestamp(r["start_date"]) <= pd.Timestamp(mkt.as_of):
        return 0.0
    months = _months_between(r["start_date"], r["end_date"])
    v = _float_swap(mkt, r["ccy"], r["direction"] == "BUY", r["notional"], r["fixed_rate"], r["start_date"],
                    r["end_date"], months)
    return v * mkt.fx(r["ccy"])


def _fixed_leg_npv(mkt, ccy, N, K, start, end, freq) -> float:
    ql = _ql()
    leg = ql.FixedRateLeg(_sched(start, end, freq), ql.Actual365Fixed(), [float(N)], [float(K)])
    cpn = ql.CashFlows.npv(leg, mkt.curve(ccy), False, mkt.ref, mkt.ref)
    return cpn + float(N) * mkt.df(ccy, end) - float(N) * mkt.df(ccy, start)


def price_xccy(r, mkt: QLMarket) -> float:
    leg1 = _fixed_leg_npv(mkt, "USD", r["notional"], r["fixed_rate"], r["start_date"], r["maturity_date"],
                          r["pay_freq_months"])
    leg2 = _fixed_leg_npv(mkt, r["ccy_2"], r["notional_2"], r["fixed_rate_2"], r["start_date"],
                          r["maturity_date"], r["pay_freq_months"])
    sgn = 1.0 if r["direction"] == "RECEIVE_CCY2" else -1.0
    return sgn * (leg2 * mkt.fx(r["ccy_2"]) - leg1)


def price_fxfwd(r, mkt: QLMarket) -> float:
    c = r["ccy"]
    sgn = 1.0 if r["direction"] == "BUY" else -1.0
    T = r["maturity_date"]
    return sgn * float(r["notional"]) * (mkt.fx(c) * mkt.df(c, T) - float(r["forward_rate"]) * mkt.df("USD", T))


def price_option(r, mkt: QLMarket) -> float:
    ql = _ql()
    ql.Settings.instance().evaluationDate = mkt.ref
    und = r["underlying"]
    ccy = mkt.eq_ccy.get(und, r["ccy"])
    mat = qdate(r["maturity_date"])
    if mat <= mkt.ref + 1:
        return 0.0
    T = (mat - mkt.ref) / 365.0
    dc = ql.Actual365Fixed()
    sigma = float(vol_at(mkt.eq_vol.get(und, {0.25: 0.25, 10.0: 0.25}), T))
    q = float(r.get("dividend_yield") if pd.notna(r.get("dividend_yield")) else 0.015)
    spot = ql.QuoteHandle(ql.SimpleQuote(float(mkt.eq_spot[und])))
    div = ql.YieldTermStructureHandle(ql.FlatForward(mkt.ref, q, dc))
    vol = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(mkt.ref, ql.NullCalendar(), sigma, dc))
    proc = ql.BlackScholesMertonProcess(spot, div, mkt.handle(ccy), vol)
    payoff = ql.PlainVanillaPayoff(ql.Option.Call if r["option_type"] == "CALL" else ql.Option.Put,
                                   float(r["strike"]))
    if r["exercise_style"] == "AMERICAN":
        opt = ql.VanillaOption(payoff, ql.AmericanExercise(mkt.ref, mat))
        opt.setPricingEngine(ql.BaroneAdesiWhaleyApproximationEngine(proc))
    else:
        opt = ql.VanillaOption(payoff, ql.EuropeanExercise(mat))
        opt.setPricingEngine(ql.AnalyticEuropeanEngine(proc))
    sgn = 1.0 if r["direction"] == "LONG" else -1.0
    return sgn * float(r["quantity"]) * opt.NPV() * mkt.fx(ccy)


PRICERS = {"irs": price_irs, "fra": price_fra, "xccy": price_xccy, "fxfwd": price_fxfwd,
           "euro_opt": price_option, "amer_opt": price_option}


def price_trade(row, mkt: QLMarket) -> float:
    r = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    if pd.Timestamp(r["maturity_date"]) <= pd.Timestamp(mkt.as_of):
        return 0.0
    return float(PRICERS[r["product"]](r, mkt))


def price_frame(df: pd.DataFrame, mkt: QLMarket) -> np.ndarray:
    out = np.full(len(df), np.nan)
    for i, (_, row) in enumerate(df.iterrows()):
        try:
            out[i] = price_trade(row, mkt)
        except Exception:          # noqa: BLE001 - recorded as NaN, reported in the cross-check
            out[i] = math.nan
    return out
