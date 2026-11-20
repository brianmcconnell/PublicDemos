"""Vectorised grid pricers: value every trade on every (time step, path) of a SimCube.

All values are returned in USD with shape (n_trades, n_steps, n_paths).
Discount factors on a path come from the analytic Hull-White bond price P(t,T | x(t)).

Conventions (shared with pricers_ql so t0 values agree)
* ACT/365F year fractions, unadjusted schedules generated forward from start_date
* cash flows on or before the valuation time are treated as paid (strict T > t)
* IRS floating leg = N [ (1 + L tau) P(t,T_next) - P(t,T_end) ] while the period that was
  current at t0 is running (L = stored current_fixing); for periods that reset after t0 the
  coupon is valued as if reset at the simulation date: N [1 - P(t,T_end)]; forward-starting:
  N [P(t,T_start) - P(t,T_end)]
* XCCY: fixed/fixed with initial (if t < start) and final notional exchange
* FRA: single-curve, N [P(t,Ts) - P(t,Te) - K tau P(t,Te)] for BUY (pay fixed), 0 after Ts
* FX forward: N [S(t) P_ccy(t,T) - K P_USD(t,T)] for BUY (buy ccy vs USD)
* Options: Black-Scholes (European) / Barone-Adesi-Whaley (American) with r = -ln P(t,T)/tau,
  sigma = historical vol term structure at tau, continuous dividend yield
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import ndtr

from .timegrid import add_months, to_years
from .vol import vol_at

_SQRT2PI = np.sqrt(2.0 * np.pi)


def _npdf(x):
    return np.exp(-0.5 * x * x) / _SQRT2PI


# ---------------------------------------------------------------------------
# Option formulas (vectorised)
# ---------------------------------------------------------------------------
def bs_price(S, K, T, r, q, vol, is_call):
    S, K, T, r, q, vol = (np.asarray(a, dtype=float) for a in (S, K, T, r, q, vol))
    sq = vol * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * vol * vol) * T) / sq
    d2 = d1 - sq
    dq, dr = np.exp(-q * T), np.exp(-r * T)
    call = S * dq * ndtr(d1) - K * dr * ndtr(d2)
    put = K * dr * ndtr(-d2) - S * dq * ndtr(-d1)
    return np.where(is_call, call, put)


def _bs_call(S, K, T, r, q, sq):
    d1 = (np.log(S / K) + (r - q) * T + 0.5 * sq * sq) / sq
    return S * np.exp(-q * T) * ndtr(d1) - K * np.exp(-r * T) * ndtr(d1 - sq)


def _bs_put(S, K, T, r, q, sq):
    d1 = (np.log(S / K) + (r - q) * T + 0.5 * sq * sq) / sq
    return K * np.exp(-r * T) * ndtr(sq - d1) - S * np.exp(-q * T) * ndtr(-d1)


def _baw_branch(S, K, T, r, q, vol, call: bool, iters: int, tol: float):
    """One BAW branch on flat 1-D arrays (Haug's Newton scheme for the critical price)."""
    b = r - q
    v2 = vol * vol
    sq = vol * np.sqrt(T)
    M, N = 2.0 * r / v2, 2.0 * b / v2
    Kf = 1.0 - np.exp(-r * T)
    carry = np.exp((b - r) * T)
    euro = _bs_call(S, K, T, r, q, sq) if call else _bs_put(S, K, T, r, q, sq)
    if call:
        qq = (-(N - 1) + np.sqrt((N - 1) ** 2 + 4 * M / Kf)) / 2
        qu = (-(N - 1) + np.sqrt((N - 1) ** 2 + 4 * M)) / 2
        su = K / (1 - 1 / qu)
        Si = K + (su - K) * (1 - np.exp(-(b * T + 2 * sq) * K / (su - K)))
    else:
        qq = (-(N - 1) - np.sqrt((N - 1) ** 2 + 4 * M / Kf)) / 2
        qu = (-(N - 1) - np.sqrt((N - 1) ** 2 + 4 * M)) / 2
        su = K / (1 - 1 / qu)
        Si = su + (K - su) * np.exp((b * T - 2 * sq) * K / (K - su))
    for _ in range(iters):
        d1 = (np.log(Si / K) + (b + 0.5 * v2) * T) / sq
        if call:
            rhs = _bs_call(Si, K, T, r, q, sq) + (1 - carry * ndtr(d1)) * Si / qq
            bi = carry * ndtr(d1) * (1 - 1 / qq) + (1 - carry * _npdf(d1) / sq) / qq
            new = np.maximum((K + rhs - bi * Si) / (1 - bi), K * 1.0000001)
        else:
            rhs = _bs_put(Si, K, T, r, q, sq) - (1 - carry * ndtr(-d1)) * Si / qq
            bi = -carry * ndtr(-d1) * (1 - 1 / qq) - (1 + carry * _npdf(-d1) / sq) / qq
            new = np.clip((K - rhs + bi * Si) / (1 + bi), 1e-12 * K, K * 0.9999999)
        done = np.nanmax(np.abs(new - Si) / K) < tol if new.size else True
        Si = new
        if done:
            break
    d1 = (np.log(Si / K) + (b + 0.5 * v2) * T) / sq
    if call:
        A = (Si / qq) * (1 - carry * ndtr(d1))
        px = np.where(S < Si, euro + A * (S / Si) ** qq, S - K)
        ok = (b < r) & (r > 1e-8) & np.isfinite(px)
    else:
        A = -(Si / qq) * (1 - carry * ndtr(-d1))
        px = np.where(S > Si, euro + A * (S / Si) ** qq, K - S)
        ok = (r > 1e-8) & np.isfinite(px)
    px = np.where(ok, px, euro)
    intrinsic = np.maximum(S - K, 0) if call else np.maximum(K - S, 0)
    return np.maximum(np.maximum(px, euro), intrinsic)


def baw_price(S, K, T, r, q, vol, is_call, iters: int = 25, tol: float = 1e-9):
    """Barone-Adesi-Whaley (1987) quadratic approximation for American options.

    Calls and puts are solved separately (only the needed branch is evaluated) and the Newton
    iteration for the critical price stops once every element has converged.
    """
    S, K, T, r, q, vol = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (S, K, T, r, q, vol)))
    is_call = np.broadcast_to(np.asarray(is_call, dtype=bool), S.shape)
    out = np.empty(S.shape)
    with np.errstate(all="ignore"):
        for call in (True, False):
            m = is_call == call
            if m.any():
                out[m] = _baw_branch(S[m], K[m], T[m], r[m], q[m], vol[m], call, iters, tol)
    return out


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------
def _dates(col) -> np.ndarray:
    return pd.to_datetime(pd.Series(col)).values.astype("datetime64[D]")


def schedule(start, maturity, freq_m, as_of):
    """Payment times matrix T (n,K), accrual tau (n,K), period-start times prev (n,K), mask."""
    s, m = _dates(start), _dates(maturity)
    freq = np.asarray(freq_m).astype(int)
    months = (m.astype("datetime64[M]") - s.astype("datetime64[M]")).astype(int)
    nper = np.maximum(1, np.round(months / freq).astype(int))
    K = int(nper.max())
    k = np.arange(1, K + 1)
    pay = np.empty((len(s), K), dtype="datetime64[D]")
    prv = np.empty((len(s), K), dtype="datetime64[D]")
    for j in range(K):
        pay[:, j] = add_months(s, (j + 1) * freq)
        prv[:, j] = add_months(s, j * freq)
    mask = k[None, :] <= nper[:, None]
    # last period ends exactly at maturity
    last = nper - 1
    pay[np.arange(len(s)), last] = m
    t0 = np.datetime64(pd.Timestamp(as_of).date(), "D")
    T = (pay - t0).astype(float) / 365.0
    P = (prv - t0).astype(float) / 365.0
    tau = (pay - prv).astype(float) / 365.0
    return np.where(mask, T, np.nan), np.where(mask, tau, 0.0), np.where(mask, P, np.nan), mask


def _P(hw, t, T, x):
    """Bond prices with T possibly <= t (returns 0 there)."""
    T = np.asarray(T, dtype=float)
    ok = np.isfinite(T) & (T > t)
    Tsafe = np.where(ok, T, t + 1.0)
    out = hw.P(t, Tsafe, x)
    return np.where(ok[..., None], out, 0.0)


def _sign(direction, positive: str):
    return np.where(np.asarray(direction) == positive, 1.0, -1.0)


# ---------------------------------------------------------------------------
# Product pricers (single currency group per call)
# ---------------------------------------------------------------------------
def price_irs(df: pd.DataFrame, cube, steps) -> np.ndarray:
    n, P = len(df), cube.n_paths
    out = np.zeros((n, len(steps), P))
    as_of = cube.as_of
    for ccy in df["ccy"].unique():
        m = (df["ccy"] == ccy).to_numpy()
        d = df[m]
        hw, x, fx = cube.hw[ccy], cube.x[ccy], cube.fx[ccy]
        T, tau, prv, mask = schedule(d["start_date"], d["maturity_date"], d["pay_freq_months"], as_of)
        ts, te = to_years(d["start_date"], as_of), to_years(d["maturity_date"], as_of)
        N = d["notional"].to_numpy(float)[:, None]
        K = d["fixed_rate"].to_numpy(float)[:, None]
        L = pd.to_numeric(d.get("current_fixing"), errors="coerce").to_numpy(float) if "current_fixing" in d else np.full(len(d), np.nan)
        lr = to_years(d["last_reset_date"], as_of) if "last_reset_date" in d else np.full(len(d), np.nan)
        sgn = _sign(d["direction"], "PAY")[:, None]          # PAY fixed: V = float - fixed
        rows = np.arange(len(d))
        vals = np.zeros((len(d), len(steps), P))
        for j, s in enumerate(steps):
            t, xs = cube.t[s], x[s]
            alive = te > t
            if not alive.any():
                continue
            PT = _P(hw, t, T, xs)                                    # (n,K,P)
            fixed = N * K * np.einsum("nk,nkp->np", tau, PT)
            Pend = _P(hw, t, te, xs)                                 # (n,P)
            Pst = _P(hw, t, ts, xs)
            active = mask & (T > t)
            nxt = np.argmax(active, axis=1)
            P_next = PT[rows, nxt]
            tau_next = tau[rows, nxt]
            prev_next = prv[rows, nxt]
            known = np.isfinite(L) & np.isfinite(lr) & (np.abs(prev_next - lr) < 1e-9)
            fl_known = N * ((1 + np.nan_to_num(L)[:, None] * tau_next[:, None]) * P_next - Pend)
            fl_reset = N * (1.0 - Pend)
            fl_fwd = N * (Pst - Pend)
            flt = np.where((ts > t)[:, None], fl_fwd, np.where(known[:, None], fl_known, fl_reset))
            v = sgn * (flt - fixed)
            vals[:, j] = np.where(alive[:, None], v, 0.0) * fx[s][None, :]
        out[m] = vals
    return out


def price_fra(df: pd.DataFrame, cube, steps) -> np.ndarray:
    n, P = len(df), cube.n_paths
    out = np.zeros((n, len(steps), P))
    as_of = cube.as_of
    for ccy in df["ccy"].unique():
        m = (df["ccy"] == ccy).to_numpy()
        d = df[m]
        hw, x, fx = cube.hw[ccy], cube.x[ccy], cube.fx[ccy]
        ts, te = to_years(d["start_date"], as_of), to_years(d["end_date"], as_of)
        tau = (te - ts)[:, None]
        N = d["notional"].to_numpy(float)[:, None]
        K = d["fixed_rate"].to_numpy(float)[:, None]
        sgn = _sign(d["direction"], "BUY")[:, None]
        vals = np.zeros((len(d), len(steps), P))
        for j, s in enumerate(steps):
            t, xs = cube.t[s], x[s]
            live = ts > t
            if not live.any():
                continue
            Ps, Pe = _P(hw, t, ts, xs), _P(hw, t, te, xs)
            v = sgn * N * (Ps - Pe - K * tau * Pe)
            vals[:, j] = np.where(live[:, None], v, 0.0) * fx[s][None, :]
        out[m] = vals
    return out


def _fixed_leg_with_notional(hw, t, xs, T, tau, K, N, ts, te):
    PT = _P(hw, t, T, xs)
    cpn = N * K * np.einsum("nk,nkp->np", tau, PT)
    fin = N * _P(hw, t, te, xs)
    ini = np.where((ts > t)[:, None], N * _P(hw, t, ts, xs), 0.0)
    return cpn + fin - ini


def price_xccy(df: pd.DataFrame, cube, steps) -> np.ndarray:
    n, P = len(df), cube.n_paths
    out = np.zeros((n, len(steps), P))
    as_of = cube.as_of
    for c2 in df["ccy_2"].unique():
        m = (df["ccy_2"] == c2).to_numpy()
        d = df[m]
        h1, h2 = cube.hw["USD"], cube.hw[c2]
        x1, x2, fx2 = cube.x["USD"], cube.x[c2], cube.fx[c2]
        T, tau, _, _ = schedule(d["start_date"], d["maturity_date"], d["pay_freq_months"], as_of)
        ts, te = to_years(d["start_date"], as_of), to_years(d["maturity_date"], as_of)
        N1 = d["notional"].to_numpy(float)[:, None]
        N2 = d["notional_2"].to_numpy(float)[:, None]
        K1 = d["fixed_rate"].to_numpy(float)[:, None]
        K2 = d["fixed_rate_2"].to_numpy(float)[:, None]
        sgn = _sign(d["direction"], "RECEIVE_CCY2")[:, None]
        vals = np.zeros((len(d), len(steps), P))
        for j, s in enumerate(steps):
            t = cube.t[s]
            alive = te > t
            if not alive.any():
                continue
            leg1 = _fixed_leg_with_notional(h1, t, x1[s], T, tau, K1, N1, ts, te)
            leg2 = _fixed_leg_with_notional(h2, t, x2[s], T, tau, K2, N2, ts, te)
            v = sgn * (leg2 * fx2[s][None, :] - leg1)
            vals[:, j] = np.where(alive[:, None], v, 0.0)
        out[m] = vals
    return out


def price_fxfwd(df: pd.DataFrame, cube, steps) -> np.ndarray:
    n, P = len(df), cube.n_paths
    out = np.zeros((n, len(steps), P))
    as_of = cube.as_of
    hu, xu = cube.hw["USD"], cube.x["USD"]
    for c in df["ccy"].unique():
        m = (df["ccy"] == c).to_numpy()
        d = df[m]
        hc, xc, fx = cube.hw[c], cube.x[c], cube.fx[c]
        T = to_years(d["maturity_date"], as_of)
        N = d["notional"].to_numpy(float)[:, None]
        K = d["forward_rate"].to_numpy(float)[:, None]
        sgn = _sign(d["direction"], "BUY")[:, None]
        vals = np.zeros((len(d), len(steps), P))
        for j, s in enumerate(steps):
            t = cube.t[s]
            live = T > t
            if not live.any():
                continue
            v = sgn * N * (fx[s][None, :] * _P(hc, t, T, xc[s]) - K * _P(hu, t, T, xu[s]))
            vals[:, j] = np.where(live[:, None], v, 0.0)
        out[m] = vals
    return out


def price_option(df: pd.DataFrame, cube, steps) -> np.ndarray:
    n, P = len(df), cube.n_paths
    out = np.zeros((n, len(steps), P))
    as_of = cube.as_of
    for und in df["underlying"].unique():
        m = (df["underlying"] == und).to_numpy()
        d = df[m]
        if und not in cube.eq:
            continue
        ccy = cube.eq_ccy.get(und, d["ccy"].iloc[0])
        hw, x, fx, S = cube.hw[ccy], cube.x[ccy], cube.fx[ccy], cube.eq[und]
        term = cube.eq_vol.get(und, {0.25: 0.25, 10.0: 0.25})
        Tm = to_years(d["maturity_date"], as_of)
        K = d["strike"].to_numpy(float)[:, None]
        qty = d["quantity"].to_numpy(float)[:, None]
        q = pd.to_numeric(d.get("dividend_yield", 0.015), errors="coerce")
        q = np.full(len(d), 0.015) if np.isscalar(q) else np.nan_to_num(np.asarray(q, float), nan=0.015)
        q = q[:, None]
        is_call = (d["option_type"].to_numpy() == "CALL")[:, None]
        amer = (d["exercise_style"].to_numpy() == "AMERICAN")
        sgn = _sign(d["direction"], "LONG")[:, None]
        vals = np.zeros((len(d), len(steps), P))
        for j, s in enumerate(steps):
            t = cube.t[s]
            live = Tm > t + 1.0 / 365.0
            if not live.any():
                continue
            tau = np.where(live, Tm - t, 1.0)[:, None]
            Pt = _P(hw, t, np.where(live, Tm, t + 1.0), x[s])
            r = -np.log(np.maximum(Pt, 1e-300)) / tau
            vol = vol_at(term, tau[:, 0])[:, None]
            Sp = S[s][None, :]
            px = np.zeros((len(d), P))
            if (~amer).any():
                e = ~amer
                px[e] = bs_price(Sp, K[e], tau[e], r[e], q[e], vol[e], is_call[e])
            if amer.any():
                px[amer] = baw_price(Sp, K[amer], tau[amer], r[amer], q[amer], vol[amer], is_call[amer])
            v = sgn * qty * px
            vals[:, j] = np.where(live[:, None], v, 0.0) * fx[s][None, :]
        out[m] = vals
    return out


PRICERS = {"irs": price_irs, "fra": price_fra, "xccy": price_xccy, "fxfwd": price_fxfwd,
           "euro_opt": price_option, "amer_opt": price_option}


def price_trades(df: pd.DataFrame, cube, steps=None) -> np.ndarray:
    """USD values (n_trades, n_steps, n_paths) in the row order of df."""
    steps = list(range(cube.n_steps)) if steps is None else list(steps)
    out = np.zeros((len(df), len(steps), cube.n_paths))
    for prod, fn in PRICERS.items():
        m = (df["product"] == prod).to_numpy()
        if m.any():
            out[m] = fn(df[m].reset_index(drop=True), cube, steps)
    return out
