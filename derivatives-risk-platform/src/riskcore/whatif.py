"""What-if trade impact on the batch risk numbers.

A hypothetical trade is priced on the same simulation cube as the daily batch (05 -> 13), so its value paths
V[step, path] line up path by path with the stored portfolio paths. Adding V to its book's paths and to its
counterparty's netting key gives exact before / after numbers, computed the same way as 13_calc_exposure:

  Book / firm   X(step, path) = sum of trade values; VaR95 / ES95 from riskcore.var.book_var
  Counterparty  E = sum over netting sets of max(sum V, 0) + sum over un-netted trades of max(V, 0)
                EE = mean over paths, PFE95 = 95th percentile over paths (uncollateralised, as in the batch)

Portfolio files, written by 13_calc_exposure to <sim_cubes>/portfolio_<as_of>/:
  books.npz        book_id[B], n_trades[B], pv[B, S, P] float64        sum of trade values per book
  cpty/<id>.npz    key[K], n_trades[K], pv[K, S, P] float32            per counterparty netting key:
                   a netting set holds the raw sum of V (floored at zero only when exposure is taken);
                   key 'NA' holds the sum of max(V, 0) over the counterparty's trades without netting
  refdata.json     books, counterparties, netting agreements, collateral agreements, equities
  manifest.json    written last; marks the directory complete (as_of, cube file, counts)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import secrets

import numpy as np
import pandas as pd

from .config import FLOAT_INDEX
from .pricers_vec import price_trades
from .sensitivity import MarketT0, compute as compute_sensitivities
from .timegrid import add_months
from .var import book_var

PORTFOLIO_PREFIX = "portfolio_"
NA = "NA"
PRODUCTS = ("irs", "xccy", "fra", "fxfwd", "euro_opt", "amer_opt")
PRODUCT_LABELS = {"irs": "Interest rate swap", "xccy": "Cross-currency swap", "fra": "FRA",
                  "fxfwd": "FX forward", "euro_opt": "European equity option", "amer_opt": "American equity option"}
DIRECTIONS = {"irs": ("PAY", "RECEIVE"), "xccy": ("RECEIVE_CCY2", "PAY_CCY2"), "fra": ("BUY", "SELL"),
              "fxfwd": ("BUY", "SELL"), "euro_opt": ("LONG", "SHORT"), "amer_opt": ("LONG", "SHORT")}
# field solved by "at market" (trade worth zero at t0); options are struck at spot instead
MARKET_FIELD = {"irs": "fixed_rate", "fra": "fixed_rate", "xccy": "fixed_rate_2", "fxfwd": "forward_rate"}
VAR_STEPS = {"1-day": 1, "10-day": 10}      # grid steps 1..10 are business days
PCT = 95


# ---------------------------------------------------------------------------
# Portfolio files (writer side: 13_calc_exposure)
# ---------------------------------------------------------------------------
def npz_bytes(**arrays) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def load_npz(blob: bytes) -> dict:
    with np.load(io.BytesIO(blob), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def books_npz(book_pdf: pd.DataFrame) -> bytes:
    """book_pdf: book_id, time_step, n_trades, pv_paths (one row per book and step, already summed)."""
    books = sorted(book_pdf["book_id"].unique())
    idx = {b: i for i, b in enumerate(books)}
    S = int(book_pdf["time_step"].max()) + 1
    P = len(book_pdf["pv_paths"].iloc[0])
    pv = np.zeros((len(books), S, P))
    n = np.zeros(len(books), dtype=np.int64)
    for b, s, k, arr in zip(book_pdf["book_id"], book_pdf["time_step"], book_pdf["n_trades"], book_pdf["pv_paths"]):
        pv[idx[b], int(s)] = np.asarray(arr, float)
        n[idx[b]] = int(k)
    return npz_bytes(book_id=np.array(books), n_trades=n, pv=pv)


def cpty_npz(pdf: pd.DataFrame) -> bytes:
    """One counterparty's PART_NS / PART_NA partial rows (stg_exposure_parts) -> summed paths per netting key."""
    key = pdf["netting_set_id"].fillna(NA).astype(str).to_numpy()
    keys = sorted(set(key))
    S = int(pdf["time_step"].max()) + 1
    P = len(pdf["pv_paths"].iloc[0])
    pv = np.zeros((len(keys), S, P))
    ki = {k: i for i, k in enumerate(keys)}
    steps = pdf["time_step"].to_numpy()
    for k, s, arr in zip(key, steps, pdf["pv_paths"]):
        pv[ki[k], int(s)] += np.asarray(arr, float)
    n = np.zeros(len(keys), dtype=np.int64)
    first = steps == steps.min()
    for k, c in zip(key[first], pdf["n_trades"].to_numpy()[first]):
        n[ki[k]] += int(c)
    return npz_bytes(key=np.array(keys), n_trades=n, pv=pv.astype(np.float32))


def refdata(books: pd.DataFrame, cptys: pd.DataFrame, netting: pd.DataFrame, csa: pd.DataFrame,
            cube) -> dict:
    """Reference data the trade-entry screen needs, as JSON-safe records."""
    def rec(df):
        return json.loads(df.to_json(orient="records", date_format="iso"))
    eq = [{"ticker": k, "ccy": cube.eq_ccy.get(k), "spot": float(a[0, 0]),
           "div_yield": float(cube.div_yield.get(k, 0.015))} for k, a in sorted(cube.eq.items())]
    return {"books": rec(books), "counterparties": rec(cptys), "netting_sets": rec(netting),
            "collateral": rec(csa), "equities": eq}


# ---------------------------------------------------------------------------
# Portfolio files (reader side: the app)
# ---------------------------------------------------------------------------
class Portfolio:
    """Book value paths of the batch run plus reference data."""

    def __init__(self, manifest: dict, books_blob: bytes, ref: dict):
        z = load_npz(books_blob)
        self.manifest = manifest
        self.as_of = dt.date.fromisoformat(manifest["as_of"])
        self.book_ids = [str(b) for b in z["book_id"]]
        self.n_trades = dict(zip(self.book_ids, map(int, z["n_trades"])))
        self.pv = z["pv"]
        self._idx = {b: i for i, b in enumerate(self.book_ids)}
        self.ref = ref
        self.S, self.P = self.pv.shape[1], self.pv.shape[2]

    def book(self, book_id: str) -> np.ndarray:
        i = self._idx.get(book_id)
        return np.zeros((self.S, self.P)) if i is None else self.pv[i]

    def firm(self) -> np.ndarray:
        return self.pv.sum(axis=0)


def cpty_paths(blob: bytes | None) -> dict:
    """{key: (n_trades, pv (S, P) float64)} for one counterparty ({} when it has no live trades)."""
    if not blob:
        return {}
    z = load_npz(blob)
    return {str(k): (int(n), z["pv"][i].astype(np.float64)) for i, (k, n) in enumerate(zip(z["key"], z["n_trades"]))}


# ---------------------------------------------------------------------------
# Trade construction
# ---------------------------------------------------------------------------
def new_trade_id(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"WI-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"


def _date(v) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _shift(d: dt.date, months: int) -> dt.date:
    """d + months on the pricer's schedule convention (riskcore.timegrid.add_months)."""
    return pd.Timestamp(add_months(np.array([np.datetime64(d, "D")]), months)[0]).date()


def _first_period_end(start: dt.date, maturity: dt.date, freq: int) -> dt.date:
    return min(_shift(start, freq), maturity)


def market_fixing(cube, ccy: str, start: dt.date, end: dt.date) -> float:
    """Simple forward over [start, end] from the t0 curve (the lifecycle fixing method)."""
    as_of = cube.as_of.date()
    t1, t2 = (start - as_of).days / 365.0, (end - as_of).days / 365.0
    crv = cube.curves[ccy]
    return float((crv.df(t1) / crv.df(t2) - 1.0) / (t2 - t1))


def build_trade(spec: dict, cube) -> pd.DataFrame:
    """One-row trade frame with every column the batch pricers read.

    spec holds the fields entered on screen (dates as date or ISO string). Trades are booked on the cube's
    valuation date. A swap whose start date is already past (e.g. a saved trade rerun on a later day) is valued as
    seasoned: its current period's fixing is taken from today's curve, since fixing history is not loaded here.
    """
    p = spec["product"]
    if p not in PRODUCTS:
        raise ValueError(f"unknown product {p}")
    as_of = cube.as_of.date()
    d = dict(trade_id=spec.get("trade_id") or "WHATIF", product=p, book_id=spec["book_id"],
             counterparty_id=spec["counterparty_id"], netting_set_id=spec.get("netting_set_id") or NA,
             collateral_id=spec.get("collateral_id") or NA, version=1, as_of_date=as_of, trade_date=as_of,
             trade_status="LIVE", direction=spec["direction"])
    if d["direction"] not in DIRECTIONS[p]:
        raise ValueError(f"direction must be one of {DIRECTIONS[p]}")
    start = _date(spec.get("start_date") or as_of)
    mat = _date(spec["maturity_date"]) if spec.get("maturity_date") else None
    if p != "fra" and (mat is None or mat <= max(start, as_of)):
        raise ValueError(f"maturity / expiry must be after the start date and the valuation date {as_of}")
    notional = float(spec.get("notional") or 0.0)

    if p == "irs":
        ccy, freq = spec["ccy"], int(spec["pay_freq_months"])
        d.update(ccy=ccy, notional=notional, fixed_rate=float(spec["fixed_rate"]), float_index=FLOAT_INDEX[ccy],
                 pay_freq_months=freq, start_date=start, maturity_date=mat, last_reset_date=None,
                 current_fixing=np.nan)
        if start <= as_of:          # current period fixes at the market (today's curve)
            k = 0
            while _shift(start, (k + 1) * freq) <= as_of:      # terminates: maturity is after as_of
                k += 1
            reset = _shift(start, k * freq)
            end = min(_shift(start, (k + 1) * freq), mat)
            d.update(last_reset_date=reset, current_fixing=market_fixing(cube, ccy, as_of, end))
    elif p == "xccy":
        c2 = spec["ccy_2"]
        fx0 = float(cube.fx[c2][0, 0])
        n2 = float(spec.get("notional_2") or notional / fx0)
        d.update(ccy="USD", ccy_2=c2, notional=notional, notional_2=n2, fx_rate_at_inception=fx0,
                 fixed_rate=float(spec["fixed_rate"]), fixed_rate_2=float(spec["fixed_rate_2"]),
                 float_index=FLOAT_INDEX["USD"], pay_freq_months=int(spec["pay_freq_months"]),
                 start_date=start, maturity_date=mat)
    elif p == "fra":
        end = _date(spec["end_date"])
        if start <= as_of or end <= start:
            raise ValueError("FRA needs as_of < start < end")
        ccy = spec["ccy"]
        d.update(ccy=ccy, notional=notional, fixed_rate=float(spec["fixed_rate"]), float_index=FLOAT_INDEX[ccy],
                 start_date=start, end_date=end, maturity_date=end, settlement_date=start)
    elif p == "fxfwd":
        ccy = spec["ccy"]
        d.update(ccy=ccy, ccy_pair=f"{ccy}USD", quote_ccy="USD", notional=notional,
                 forward_rate=float(spec["forward_rate"]), start_date=as_of, maturity_date=mat)
    else:
        und = spec["underlying"]
        if und not in cube.eq:
            raise ValueError(f"{und} is not simulated")
        strike, qty = float(spec["strike"]), float(spec["quantity"])
        q = spec.get("dividend_yield")
        d.update(underlying=und, ccy=cube.eq_ccy.get(und, "USD"), strike=strike, quantity=qty,
                 notional=round(strike * qty, 2), option_type=spec["option_type"],
                 exercise_style="EUROPEAN" if p == "euro_opt" else "AMERICAN",
                 dividend_yield=float(cube.div_yield.get(und, 0.015) if q in (None, "") else q),
                 start_date=as_of, maturity_date=mat)
    return pd.DataFrame([d])


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def value_t0(trade: pd.DataFrame, cube) -> float:
    return float(np.nan_to_num(price_trades(trade, cube, steps=[0]))[0, 0].mean())


def solve_at_market(spec: dict, cube, field: str | None = None) -> float:
    """Value of `field` that makes the trade worth zero today (pricers are linear in the rate / forward)."""
    field = field or MARKET_FIELD[spec["product"]]
    if field == "forward_rate":
        s = float(cube.fx[spec["ccy"]][0, 0])
        a, b = 0.9 * s, 1.1 * s
    else:
        a, b = 0.0, 0.01
    va = value_t0(build_trade({**spec, field: a}, cube), cube)
    vb = value_t0(build_trade({**spec, field: b}, cube), cube)
    if abs(vb - va) < 1e-12:
        raise ValueError(f"trade value does not depend on {field}")
    return a - va * (b - a) / (vb - va)


def market_snapshot(cube) -> dict:
    """t0 market used for defaults on the entry screen."""
    return {"as_of": cube.as_of.date(), "fx": {c: float(a[0, 0]) for c, a in cube.fx.items() if c != "USD"},
            "eq": {k: float(a[0, 0]) for k, a in cube.eq.items()}, "eq_ccy": dict(cube.eq_ccy),
            "ccys": sorted(cube.hw)}


def price_paths(trade: pd.DataFrame, cube) -> np.ndarray:
    """(S, P) USD value of the trade on every step and path of the cube."""
    return np.nan_to_num(price_trades(trade, cube)[0], nan=0.0, posinf=0.0, neginf=0.0)


def sensitivities(trade: pd.DataFrame, cube) -> tuple[float, pd.DataFrame]:
    """(MTM, sensitivities) with the batch bump definitions (riskcore.sensitivity)."""
    mtm, sens = compute_sensitivities(trade, MarketT0(cube))
    return float(mtm["mtm_usd"].iloc[0]), sens.drop(columns=["trade_id"])


# ---------------------------------------------------------------------------
# Before / after
# ---------------------------------------------------------------------------
def _profile(E: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return E.mean(axis=1), np.percentile(E, PCT, axis=1)


def _epe(ee: np.ndarray, t: np.ndarray, horizon: float = 1.0) -> float:
    """Time-averaged EE over [0, horizon] (trapezoid on the simulation grid)."""
    m = t <= horizon + 1e-9
    tt, ee_ = t[m], ee[m]
    trap = getattr(np, "trapezoid", None) or np.trapz
    return float(trap(ee_, tt) / tt[-1]) if len(tt) > 1 and tt[-1] > 0 else float(ee_[0])


def _var_summary(X: np.ndarray) -> dict:
    v = book_var("x", X)
    out = {"t0_pv": float(v["t0_pv"].iloc[0])}
    for lab, s in VAR_STEPS.items():
        if s < len(v):
            out[f"var_{lab}"] = float(v["var_95"].iloc[s])
            out[f"es_{lab}"] = float(v["expected_shortfall_95"].iloc[s])
    return out


def counterparty_exposure(parts: dict, S: int, P: int, add: np.ndarray | None = None,
                          key: str | None = None) -> tuple[np.ndarray, np.ndarray | None]:
    """Counterparty exposure paths (S, P), optionally with trade values `add` booked under netting key `key`.
    Also returns the exposure of that netting key alone."""
    E = np.zeros((S, P))
    key_E = None
    keys = set(parts) | ({key} if key is not None and add is not None else set())
    for k in keys:
        raw = parts[k][1] if k in parts else np.zeros((S, P))
        if add is not None and k == key:
            raw = raw + (np.maximum(add, 0.0) if k == NA else add)
        Ek = raw if k == NA else np.maximum(raw, 0.0)
        E += Ek
        if k == key:
            key_E = Ek
    return E, key_E


def impact(V: np.ndarray, trade: pd.DataFrame, port: Portfolio, parts: dict, t_years: np.ndarray,
           dates: list) -> dict:
    """Before / after risk for the trade's book, the firm and the trade's counterparty."""
    tr = trade.iloc[0]
    b, key = tr["book_id"], (tr["netting_set_id"] or NA)
    S, P = V.shape
    X0 = port.book(b)
    F0 = port.firm()
    E0, K0 = counterparty_exposure(parts, S, P, None, key)
    E1, K1 = counterparty_exposure(parts, S, P, V, key)
    if K0 is None:
        K0 = np.zeros((S, P))
    ee0, pfe0 = _profile(E0)
    ee1, pfe1 = _profile(E1)
    kee0, kpfe0 = _profile(K0)
    kee1, kpfe1 = _profile(K1)
    tee, tpfe = _profile(np.maximum(V, 0.0))
    profile = pd.DataFrame({"time_step": np.arange(S), "sim_date": [pd.Timestamp(d).date() for d in dates],
                            "t_years": t_years, "ee_before": ee0, "ee_after": ee1, "pfe_before": pfe0,
                            "pfe_after": pfe1, "ns_ee_before": kee0, "ns_ee_after": kee1,
                            "ns_pfe_before": kpfe0, "ns_pfe_after": kpfe1, "trade_ee": tee, "trade_pfe": tpfe})
    cp = {"before": {"peak_pfe": float(pfe0.max()), "peak_ee": float(ee0.max()), "epe_1y": _epe(ee0, t_years)},
          "after": {"peak_pfe": float(pfe1.max()), "peak_ee": float(ee1.max()), "epe_1y": _epe(ee1, t_years)},
          "trade": {"peak_pfe": float(tpfe.max()), "peak_ee": float(tee.max()), "epe_1y": _epe(tee, t_years)},
          "netting_key": key, "n_trades_before": int(sum(n for n, _ in parts.values()))}
    s1 = VAR_STEPS["1-day"]
    return {
        "book": {"book_id": b, "n_trades_before": port.n_trades.get(b, 0),
                 "before": _var_summary(X0), "after": _var_summary(X0 + V)},
        "firm": {"before": _var_summary(F0), "after": _var_summary(F0 + V)},
        "trade": _var_summary(V),
        "counterparty": cp,
        "profile": profile,
        "pnl_1d": {"book_before": X0[s1] - X0[0], "book_after": X0[s1] + V[s1] - X0[0] - V[0]},
    }


def summary_json(mtm: float, res: dict) -> dict:
    """Headline numbers stored with each run in the what-if runs table."""
    return {"mtm_usd": mtm, "book": res["book"], "firm": res["firm"], "trade": res["trade"],
            "counterparty": res["counterparty"]}
