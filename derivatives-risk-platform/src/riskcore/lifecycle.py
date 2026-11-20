"""Trade lifecycle: book new trades at market each trading day, reset swaps from the market curve, mature trades.

Trading days are the dates in gold_zero_curves (the FRED business calendar), so every trade is booked on a
day that has a market. Trades are never deleted: at maturity they are marked trade_status = 'MATURED' with
status_date = the maturity (FRA: settlement) date, and a MATURED event is written.

New trades are struck at the market of their trade date:
* IRS / XCCY fixed rates = par rate on that day's zero curve (+ a +-2bp client spread)
* FRA rate = simple forward between start and end on that day's curve (+-1bp)
* FX forward = spot x DF_ccy / DF_usd on that day (+-5bp of the outright)
* option strikes around that day's spot
So a new trade is worth roughly zero on its trade date, as on a real blotter.

Rate fixings (IRS floating resets and FRA settlement) are the simple forward rate for the period
[reset, reset + period] taken from the latest curve on or before the reset date, interpolated along the
curve (riskcore.curves.ZeroCurve, linear in zero rate). With a reset on a curve date this is the term rate
(1 / DF(tau) - 1) / tau of that day's curve, which is what gold_rate_fixings stores.

The IRS reset schedule matches pricers_vec.schedule: period k starts at add_months(start, k * freq), with
nper = round(months / freq) periods, so the pricer recognises (last_reset_date, current_fixing) as the
running coupon.

Volumes (DAILY_NEW, per trading day) are sized for a smaller global bank and follow each asset class's
share of OTC dealer activity by trade count: FX forwards are the most numerous and shortest-dated,
IRS next, then listed-style equity options, with few FRAs (post-LIBOR) and cross-currency swaps.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .config import COUNTRY_INDEX, EQUITY_CCY, FLOAT_INDEX, FX_CCYS, PENCE_TICKERS
from .curves import ZeroCurve
from .timegrid import add_months
from .tradegen import CCY_W, PREFIX, PRODUCTS, _assign_common, _notional, _par_rate, inject_defects

LIVE, MATURED = "LIVE", "MATURED"
DAILY_NEW = {"fxfwd": 600, "irs": 80, "euro_opt": 80, "amer_opt": 30, "fra": 20, "xccy": 10}
DEFECT_RATE = 0.005                     # share of new tickets with a data defect (-> quarantine)
HISTORY_YEARS = 3
# longest possible life in days (trade date -> maturity) per product, used to skip bookings that
# could not still be alive at the opening date
MAX_LIFE_DAYS = {"irs": 3700 + 400, "xccy": 3700, "fra": 560, "fxfwd": 745, "euro_opt": 745, "amer_opt": 745}
FIXING_TENORS = (1, 3, 6, 12)           # months, for gold_rate_fixings
DATE_COLS = ["as_of_date", "trade_date", "start_date", "maturity_date", "end_date", "settlement_date",
             "last_reset_date", "status_date"]
EVENT_COLS = ["event_date", "trade_id", "product", "event_type", "effective_date", "curve_date",
              "market_level", "amount", "amount_ccy"]


# ---------------------------------------------------------------------------
# Market history
# ---------------------------------------------------------------------------
class MarketHistory:
    """Daily curves, FX and equity closes, looked up 'as of' a date (latest observation on or before it).

    zc: (as_of_date, ccy, tenor_label, zero_rate); fx: (obs_date, ccy, value); eq: (trade_date, ticker, adj_close).
    FX values and equity closes are used exactly as 05_mc_simulation uses them, so trades are struck in the
    same quote conventions the pricer simulates.
    """

    def __init__(self, zc: pd.DataFrame, fx: pd.DataFrame, eq: pd.DataFrame):
        zc = zc.assign(as_of_date=pd.to_datetime(zc["as_of_date"]).values.astype("datetime64[D]"))
        self.dates = np.array(sorted(zc["as_of_date"].unique()), dtype="datetime64[D]")
        self._zc = {d: dict(zip(g.tenor_label, g.zero_rate)) for d, g in
                    ((k, v) for k, v in zc.groupby(["as_of_date", "ccy"]))}
        self._curves: dict = {}
        fxw = fx.pivot_table(index="obs_date", columns="ccy", values="value").sort_index().ffill()
        self._fx_idx = pd.to_datetime(fxw.index).values.astype("datetime64[D]")
        self._fx = fxw
        eqw = eq.pivot_table(index="trade_date", columns="ticker", values="adj_close").sort_index().ffill()
        for t in PENCE_TICKERS:
            if t in eqw:
                eqw[t] = eqw[t] / 100.0
        self._eq_idx = pd.to_datetime(eqw.index).values.astype("datetime64[D]")
        self._eq = eqw

    @staticmethod
    def _pos(index, d):
        return np.clip(np.searchsorted(index, np.asarray(d, dtype="datetime64[D]"), "right") - 1, 0, None)

    def curve_date(self, d):
        return self.dates[self._pos(self.dates, d)]

    def curves(self, d) -> dict:
        cd = np.datetime64(self.curve_date(d), "D")
        if cd not in self._curves:
            self._curves[cd] = {c: ZeroCurve.from_tenors(z) for (dd, c), z in self._zc.items() if dd == cd}
        return self._curves[cd]

    def fx_spot(self, d) -> dict:
        row = self._fx.iloc[int(self._pos(self._fx_idx, d))]
        return {c: float(row[c]) if c in row and pd.notna(row[c]) else 1.0 for c in FX_CCYS}

    def eq_spot(self, d) -> dict:
        row = self._eq.iloc[int(self._pos(self._eq_idx, d))]
        return {t: float(v) for t, v in row.items() if pd.notna(v) and v > 0}

    def fx_level(self, ccys, dates) -> np.ndarray:
        pos = self._pos(self._fx_idx, dates)
        cols = [self._fx.columns.get_loc(c) if c in self._fx.columns else -1 for c in ccys]
        vals = self._fx.values
        return np.array([vals[p, c] if c >= 0 else np.nan for p, c in zip(pos, cols)], dtype=float)

    def eq_level(self, tickers, dates) -> np.ndarray:
        pos = self._pos(self._eq_idx, dates)
        cols = [self._eq.columns.get_loc(t) if t in self._eq.columns else -1 for t in tickers]
        vals = self._eq.values
        return np.array([vals[p, c] if c >= 0 else np.nan for p, c in zip(pos, cols)], dtype=float)

    def fixings(self, ccys, reset_dates, months):
        """Market fixing for each (ccy, reset date, period months): simple forward over the period from the
        latest curve on or before the reset date. Returns (rates, curve_dates)."""
        reset = np.asarray(reset_dates, dtype="datetime64[D]")
        months = np.asarray(months).astype(int)
        out = np.full(len(reset), np.nan)
        if len(reset) == 0:
            return out, reset
        cd = self.curve_date(reset)
        end = add_months(reset, months)
        keys = pd.DataFrame({"c": np.asarray(ccys, dtype=object), "d": cd})
        for (c, d), idx in keys.groupby(["c", "d"]).indices.items():
            crv = self.curves(d).get(c)
            if crv is None:
                continue
            d64 = np.datetime64(d, "D")
            t1 = (reset[idx] - d64).astype(float) / 365.0
            t2 = (end[idx] - d64).astype(float) / 365.0
            out[idx] = (crv.df(t1) / crv.df(t2) - 1.0) / (t2 - t1)
        return out, cd

    def fixing_table(self, dates=None) -> pd.DataFrame:
        """gold_rate_fixings rows: every curve date x ccy x FIXING_TENORS."""
        dates = self.dates if dates is None else np.asarray(dates, dtype="datetime64[D]")
        ccys = sorted({c for (_, c) in self._zc})
        g = pd.MultiIndex.from_product([dates, ccys, FIXING_TENORS], names=["d", "ccy", "m"]).to_frame(index=False)
        rate, cd = self.fixings(g.ccy.values, g.d.values, g.m.values)
        return pd.DataFrame({"fixing_date": pd.to_datetime(g.d).dt.date, "ccy": g.ccy,
                             "float_index": g.ccy.map(FLOAT_INDEX), "tenor_months": g.m.astype(int),
                             "fixing_rate": rate, "curve_date": pd.to_datetime(cd).date,
                             "method": "CURVE_SIMPLE_FORWARD"})


# ---------------------------------------------------------------------------
# New trades booked on a trading day, struck at that day's market
# ---------------------------------------------------------------------------
def _d64(d) -> np.datetime64:
    return np.datetime64(pd.Timestamp(d).date(), "D")


def _spot(d, n):
    return np.full(n, np.busday_offset(_d64(d), 2, roll="forward"))


def _yf(dates, d):
    return (np.asarray(dates, dtype="datetime64[D]") - _d64(d)).astype(float) / 365.0


def _ts(a):
    return pd.to_datetime(np.asarray(a, dtype="datetime64[D]"))


def _common(product, n, d, rng, ref):
    df = _assign_common(n, rng, ref)
    stamp = pd.Timestamp(d).strftime("%Y%m%d")
    df.insert(0, "trade_id", [f"{PREFIX[product]}-{stamp}-{i:04d}" for i in range(1, n + 1)])
    df.insert(1, "product", product)
    df["version"] = 1
    df["as_of_date"] = pd.Timestamp(d)
    df["trade_date"] = pd.Timestamp(d)
    return df


def _ccy(rng, n):
    return rng.choice(list(CCY_W), n, p=list(CCY_W.values()))


def _par_by_ccy(curves, ccy, start, mat, freq, d):
    out = np.empty(len(ccy))
    for c in np.unique(ccy):
        m = ccy == c
        out[m] = _par_rate(curves[c], _yf(start[m], d), _yf(mat[m], d), freq[m])
    return out


def new_irs(n, d, mkt, rng, ref):
    df = _common("irs", n, d, rng, ref)
    tenor = rng.choice([1, 2, 3, 5, 7, 10], n, p=[0.10, 0.15, 0.15, 0.25, 0.15, 0.20])
    fwd_m = np.where(rng.random(n) < 0.10, rng.choice([1, 3, 6, 12], n), 0)     # 10% forward-starting
    start = add_months(_spot(d, n), fwd_m)
    mat = add_months(start, tenor * 12)
    freq = rng.choice([3, 6, 12], n, p=[0.4, 0.4, 0.2])
    ccy = _ccy(rng, n)
    par = _par_by_ccy(mkt.curves(d), ccy, start, mat, freq, d)
    df["ccy"] = ccy
    df["notional"] = _notional(rng, n)
    df["fixed_rate"] = np.round(par + rng.normal(0, 0.0002, n), 6)
    df["float_index"] = [FLOAT_INDEX[c] for c in ccy]
    df["pay_freq_months"] = freq
    df["start_date"] = _ts(start)
    df["maturity_date"] = _ts(mat)
    df["last_reset_date"] = pd.NaT
    df["current_fixing"] = np.nan
    df["direction"] = rng.choice(["PAY", "RECEIVE"], n)
    return df


def new_xccy(n, d, mkt, rng, ref):
    df = _common("xccy", n, d, rng, ref)
    curves, fx = mkt.curves(d), mkt.fx_spot(d)
    tenor = rng.choice([2, 3, 5, 7, 10], n, p=[0.15, 0.2, 0.3, 0.15, 0.2])
    start = _spot(d, n)
    mat = add_months(start, tenor * 12)
    freq = rng.choice([6, 12], n)
    ccy2 = rng.choice(FX_CCYS, n, p=[0.4, 0.25, 0.15, 0.1, 0.1])
    notional = _notional(rng, n, median=20e6, sigma=0.8)
    fx0 = np.array([fx[c] for c in ccy2])
    k1 = _par_by_ccy(curves, np.full(n, "USD"), start, mat, freq, d)
    k2 = _par_by_ccy(curves, ccy2, start, mat, freq, d)
    df["ccy"] = "USD"
    df["ccy_2"] = ccy2
    df["notional"] = notional
    df["notional_2"] = np.round(notional / fx0, 2)
    df["fx_rate_at_inception"] = np.round(fx0, 6)
    df["fixed_rate"] = np.round(k1 + rng.normal(0, 0.0002, n), 6)
    df["fixed_rate_2"] = np.round(k2 + rng.normal(0, 0.0002, n), 6)
    df["float_index"] = "SOFR"
    df["pay_freq_months"] = freq
    df["start_date"] = _ts(start)
    df["maturity_date"] = _ts(mat)
    df["direction"] = rng.choice(["RECEIVE_CCY2", "PAY_CCY2"], n)
    return df


def new_fra(n, d, mkt, rng, ref):
    df = _common("fra", n, d, rng, ref)
    off = rng.choice([1, 2, 3, 6, 9, 12], n, p=[0.15, 0.2, 0.25, 0.2, 0.1, 0.1])
    period = rng.choice([3, 6], n)
    start = add_months(_spot(d, n), off)
    end = add_months(start, period)
    ccy = _ccy(rng, n)
    fwd = np.empty(n)
    curves = mkt.curves(d)
    for c in np.unique(ccy):
        m = ccy == c
        ts, te = _yf(start[m], d), _yf(end[m], d)
        fwd[m] = (curves[c].df(ts) / curves[c].df(te) - 1.0) / (te - ts)
    df["ccy"] = ccy
    df["notional"] = _notional(rng, n, median=10e6, sigma=0.8)
    df["fixed_rate"] = np.round(fwd + rng.normal(0, 0.0001, n), 6)
    df["float_index"] = [FLOAT_INDEX[c] for c in ccy]
    df["start_date"] = _ts(start)
    df["end_date"] = _ts(end)
    df["maturity_date"] = _ts(end)
    df["settlement_date"] = _ts(start)
    df["direction"] = rng.choice(["BUY", "SELL"], n)
    return df


def new_fxfwd(n, d, mkt, rng, ref):
    df = _common("fxfwd", n, d, rng, ref)
    curves, fx = mkt.curves(d), mkt.fx_spot(d)
    base = rng.choice(FX_CCYS, n, p=[0.35, 0.25, 0.2, 0.1, 0.1])
    days = rng.choice([7, 14, 30, 61, 91, 182, 273, 365, 730], n,
                      p=[0.10, 0.10, 0.20, 0.15, 0.17, 0.12, 0.05, 0.08, 0.03])
    mat = np.busday_offset(_spot(d, n) + days.astype("timedelta64[D]"), 0, roll="forward")
    T = _yf(mat, d)
    fwd = np.empty(n)
    for c in np.unique(base):
        m = base == c
        fwd[m] = fx[c] * curves[c].df(T[m]) / curves["USD"].df(T[m])
    df["ccy_pair"] = [f"{c}USD" for c in base]
    df["ccy"] = base
    df["quote_ccy"] = "USD"
    df["notional"] = _notional(rng, n, median=5e6, sigma=0.9)
    df["forward_rate"] = np.round(fwd * np.exp(rng.normal(0, 0.0005, n)), 6)
    df["start_date"] = pd.Timestamp(d)
    df["maturity_date"] = _ts(mat)
    df["direction"] = rng.choice(["BUY", "SELL"], n)
    return df


def new_option(product, n, d, mkt, rng, ref):
    df = _common(product, n, d, rng, ref)
    eq = mkt.eq_spot(d)
    tickers = np.array(sorted(eq))
    w = np.array([3.0 if t in COUNTRY_INDEX.values() else 1.0 for t in tickers])
    und = rng.choice(tickers, n, p=w / w.sum())
    spot = np.array([eq[t] for t in und])
    strike = np.round(spot * np.exp(rng.normal(0, 0.08, n)), 4)
    months = rng.choice([1, 2, 3, 6, 12, 24], n, p=[0.15, 0.2, 0.25, 0.2, 0.12, 0.08])
    mat = np.busday_offset(add_months(np.full(n, _d64(d)), months), 0, roll="forward")
    qty = np.maximum(1, np.round(_notional(rng, n, median=2e6, sigma=0.9) / strike))
    df["underlying"] = und
    df["ccy"] = [EQUITY_CCY.get(t, "USD") for t in und]
    df["strike"] = strike
    df["quantity"] = qty
    df["notional"] = np.round(qty * strike, 2)
    df["option_type"] = rng.choice(["CALL", "PUT"], n)
    df["direction"] = rng.choice(["LONG", "SHORT"], n)
    df["exercise_style"] = "AMERICAN" if product == "amer_opt" else "EUROPEAN"
    df["dividend_yield"] = 0.015
    df["start_date"] = pd.Timestamp(d)
    df["maturity_date"] = _ts(mat)
    return df


_GEN = {"irs": new_irs, "xccy": new_xccy, "fra": new_fra, "fxfwd": new_fxfwd,
        "euro_opt": lambda n, d, m, r, f: new_option("euro_opt", n, d, m, r, f),
        "amer_opt": lambda n, d, m, r, f: new_option("amer_opt", n, d, m, r, f)}


def generate_day(d, mkt: MarketHistory, ref: dict, products=None, defects: bool = True, seed: int = 11,
                 volumes: dict | None = None):
    """New tickets booked on trading day d -> ({product: DataFrame}, defect log). Deterministic per (seed, d)."""
    volumes = volumes or DAILY_NEW
    out, logs = {}, []
    day = int(pd.Timestamp(d).toordinal())
    for i, p in enumerate(PRODUCTS):
        if products is not None and p not in products:
            continue
        n = int(volumes.get(p, 0))
        if n <= 0:
            continue
        rng = np.random.default_rng([seed, day, i])
        df = _GEN[p](n, d, mkt, rng, ref)
        if defects:
            k = int(rng.binomial(n, DEFECT_RATE))
            if k:
                df, lg = inject_defects(df, p, k, rng)
                lg["as_of_date"] = pd.Timestamp(d).date()
                logs.append(lg)
        df["trade_status"] = LIVE
        df["status_date"] = pd.NaT
        out[p] = normalise(df)
    log = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame(columns=["trade_id", "product", "defect", "as_of_date"])
    return out, log


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Date columns -> datetime64[ns] (fast vector comparisons), fresh RangeIndex."""
    df = df.reset_index(drop=True)
    for c in DATE_COLS:
        if c in df:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Daily processing: maturities and resets
# ---------------------------------------------------------------------------
def _events(n, **cols) -> pd.DataFrame:
    base = {c: [None] * n for c in EVENT_COLS}
    seq = (list, tuple, np.ndarray, pd.Series, pd.Index)
    base.update({k: (list(v) if isinstance(v, seq) else [v] * n) for k, v in cols.items()})
    return pd.DataFrame(base)[EVENT_COLS]


def _sgn(direction, positive):
    return np.where(np.asarray(direction) == positive, 1.0, -1.0)


def _maturity_values(p, sub, mkt):
    """(market_level, amount, amount_ccy, curve_date) recorded on the MATURED event."""
    n = len(sub)
    none = np.full(n, np.nan)
    if p == "fra":
        s = sub["start_date"].values.astype("datetime64[D]")
        e = sub["end_date"].values.astype("datetime64[D]")
        months = (e.astype("datetime64[M]") - s.astype("datetime64[M]")).astype(int)
        L, cd = mkt.fixings(sub["ccy"].values, s, months)
        tau = (e - s).astype(float) / 365.0
        K = sub["fixed_rate"].to_numpy(float)
        amt = _sgn(sub["direction"], "BUY") * sub["notional"].to_numpy(float) * (L - K) * tau / (1 + L * tau)
        return L, amt, sub["ccy"].values, cd
    if p == "fxfwd":
        S = mkt.fx_level(sub["ccy"].values, sub["maturity_date"].values)
        amt = _sgn(sub["direction"], "BUY") * sub["notional"].to_numpy(float) * (S - sub["forward_rate"].to_numpy(float))
        return S, amt, np.full(n, "USD"), np.full(n, None)
    if p in ("euro_opt", "amer_opt"):
        S = mkt.eq_level(sub["underlying"].values, sub["maturity_date"].values)
        K = sub["strike"].to_numpy(float)
        intrinsic = np.where(sub["option_type"].values == "CALL", np.maximum(S - K, 0), np.maximum(K - S, 0))
        amt = _sgn(sub["direction"], "LONG") * sub["quantity"].to_numpy(float) * intrinsic
        return S, amt, sub["ccy"].values, np.full(n, None)
    return none, none, sub["ccy"].values, np.full(n, None)


def mature(df: pd.DataFrame, p: str, D, mkt) -> pd.DataFrame:
    """Mark LIVE trades whose maturity (FRA: settlement) date is on or before D as MATURED. Never deletes."""
    D = pd.Timestamp(D)
    mcol = "settlement_date" if p == "fra" else "maturity_date"
    due = (df["trade_status"].values == LIVE) & (df[mcol].values <= D.to_datetime64())
    if not due.any():
        return _events(0)
    sub = df.loc[due]
    lvl, amt, ccy, cd = _maturity_values(p, sub, mkt)
    df.loc[due, "trade_status"] = MATURED
    df.loc[due, "status_date"] = sub[mcol].values
    df.loc[due, "as_of_date"] = D
    return _events(len(sub), event_date=D.date(), trade_id=sub["trade_id"].values, product=p,
                   event_type=MATURED, effective_date=pd.to_datetime(sub[mcol]).dt.date.values,
                   curve_date=[pd.Timestamp(x).date() if x is not None and pd.notna(x) else None for x in cd],
                   market_level=lvl, amount=amt, amount_ccy=ccy)


def reset_irs(df: pd.DataFrame, D, mkt) -> pd.DataFrame:
    """Set (last_reset_date, current_fixing) of every LIVE IRS to the period running on D, fixing from the market."""
    D = pd.Timestamp(D)
    d64 = np.datetime64(D.date(), "D")
    ok = ((df["trade_status"].values == LIVE) & (df["start_date"].values <= D.to_datetime64())
          & df["maturity_date"].notna().values & df["pay_freq_months"].notna().values
          & (df["maturity_date"].values > D.to_datetime64()))
    idx = np.flatnonzero(ok)
    if not len(idx):
        return _events(0)
    s = df["start_date"].values[idx].astype("datetime64[D]")
    m = df["maturity_date"].values[idx].astype("datetime64[D]")
    f = df["pay_freq_months"].values[idx].astype(int)
    nper = np.maximum(1, np.round((m.astype("datetime64[M]") - s.astype("datetime64[M]")).astype(int) / f).astype(int))
    el = (d64.astype("datetime64[M]") - s.astype("datetime64[M]")).astype(int)
    k = np.clip(el // f, 0, nper - 1)
    lr = add_months(s, k * f)
    back = lr > d64
    k[back] = np.maximum(k[back] - 1, 0)
    lr = add_months(s, k * f)
    cur = df["last_reset_date"].values[idx].astype("datetime64[D]")
    chg = np.isnat(cur) | (cur != lr)
    if not chg.any():
        return _events(0)
    sel = idx[chg]
    rate, cd = mkt.fixings(df["ccy"].values[sel], lr[chg], f[chg])
    col = df.columns.get_loc
    df.iloc[sel, col("last_reset_date")] = pd.to_datetime(lr[chg])
    df.iloc[sel, col("current_fixing")] = np.round(rate, 6)
    df.iloc[sel, col("as_of_date")] = D
    return _events(len(sel), event_date=D.date(), trade_id=df["trade_id"].values[sel], product="irs",
                   event_type="RESET", effective_date=pd.to_datetime(lr[chg]).date,
                   curve_date=pd.to_datetime(cd).date, market_level=np.round(rate, 6), amount=np.nan,
                   amount_ccy=df["ccy"].values[sel])


def process_day(state: dict, D, mkt) -> pd.DataFrame:
    """Maturities for every product, then IRS resets, on trading day D. Mutates state; returns events."""
    ev = [mature(df, p, D, mkt) for p, df in state.items()]
    if "irs" in state:
        ev.append(reset_irs(state["irs"], D, mkt))
    ev = [e for e in ev if len(e)]
    return pd.concat(ev, ignore_index=True) if ev else _events(0)


def new_events(new: dict, D) -> pd.DataFrame:
    ev = [_events(len(df), event_date=pd.Timestamp(D).date(), trade_id=df["trade_id"].values, product=p,
                  event_type="NEW", effective_date=df["start_date"].dt.date.values,
                  amount=df["notional"].to_numpy(float), amount_ccy=df["ccy"].values)
          for p, df in new.items() if len(df)]
    return pd.concat(ev, ignore_index=True) if ev else _events(0)


# ---------------------------------------------------------------------------
# Opening book and multi-day runs
# ---------------------------------------------------------------------------
def opening_book(T0, mkt: MarketHistory, ref: dict, seed: int = 11, volumes: dict | None = None,
                 log_every: int = 250) -> dict:
    """Book still alive on T0, rebuilt by running the same daily booking over every trading day in the
    market history before T0 (each trade struck on its own trade-date market) and keeping only trades that
    have not matured by T0. No defects (they belong to the daily flow)."""
    T0 = pd.Timestamp(T0)
    parts = {p: [] for p in PRODUCTS}
    days = mkt.dates[mkt.dates < np.datetime64(T0.date(), "D")]
    for i, d in enumerate(days):
        age = (np.datetime64(T0.date(), "D") - d).astype(int)
        prods = [p for p in PRODUCTS if age < MAX_LIFE_DAYS[p]]
        if not prods:
            continue
        new, _ = generate_day(d, mkt, ref, products=prods, defects=False, seed=seed, volumes=volumes)
        for p, df in new.items():
            mcol = "settlement_date" if p == "fra" else "maturity_date"
            keep = df[df[mcol] > T0]
            if len(keep):
                parts[p].append(keep)
        if log_every and i % log_every == 0:
            print(f"  opening book: {d} ({i + 1}/{len(days)})")
    return {p: normalise(pd.concat(v, ignore_index=True)) for p, v in parts.items() if v}


def run_days(state: dict, days, mkt: MarketHistory, ref: dict, seed: int = 11, volumes: dict | None = None,
             log_every: int = 50):
    """Run the daily lifecycle over trading days: book that day's new trades, then mature / reset.
    Returns (state, events, defect_log). New trades are appended to state."""
    events, logs = [], []
    pending = {p: [] for p in PRODUCTS}
    for i, D in enumerate(days):
        new, log = generate_day(D, mkt, ref, seed=seed, volumes=volumes)
        events.append(new_events(new, D))
        if len(log):
            logs.append(log)
        for p, df in new.items():
            state[p] = normalise(pd.concat([state[p], df], ignore_index=True)) if p in state else df
        events.append(process_day(state, D, mkt))
        if log_every and i % log_every == 0:
            live = sum(int((df["trade_status"] == LIVE).sum()) for df in state.values())
            print(f"  {pd.Timestamp(D).date()}: live {live:,}")
    ev = pd.concat([e for e in events if len(e)], ignore_index=True) if events else _events(0)
    lg = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame(columns=["trade_id", "product", "defect", "as_of_date"])
    return state, ev, lg


def to_spark_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Datetime columns -> python dates (None for NaT) for writing to DATE columns."""
    out = df.copy()
    for c in DATE_COLS:
        if c in out:
            s = pd.to_datetime(out[c], errors="coerce")
            out[c] = pd.Series(s.dt.date, index=out.index).where(s.notna(), None)
    return out


def live_filter_sql(as_of) -> str:
    """SQL predicate for trades a pricing run on as_of should include."""
    return (f"coalesce(trade_status, 'LIVE') = 'LIVE' AND (maturity_date IS NULL OR maturity_date > DATE'{as_of}') "
            f"AND (trade_date IS NULL OR trade_date <= DATE'{as_of}')")
