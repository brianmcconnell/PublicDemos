"""Trade sensitivities (greeks) by bump-and-reprice at the valuation date.

Every scenario is a one-step SimCube at t = 0 built from step 0 of the latest simulation cube, so trades are
valued with exactly the same pricers (pricers_vec) and market data as the exposure and VaR runs.

Scenarios
  * spot cube, 5 paths: base | all FX +1% | all FX -1% | all equities +1% | all equities -1%
    Each trade depends on at most one non-USD FX rate and at most one equity, so bumping every FX rate (or every
    equity) together still gives each trade's own delta and gamma.
  * vol cube, 1 path: every equity vol term point +1 vol point (0.01)
  * IR cubes, 1 path each, per currency: the whole zero curve +1bp (parallel) and each curve node +1bp
    (key-rate buckets: the curve is linear in zero rate between nodes, so the node bumps are triangular and sum
    to the parallel bump). Only trades exposed to that currency's curve are repriced.

Sensitivities (all USD, "cash" greeks):
  FX_DELTA  = (V(fx+1%) - V(fx-1%)) / 2         change for a +1% move in the FX rate (USD per unit of ccy)
  FX_GAMMA  = V(fx+1%) + V(fx-1%) - 2 V          change in FX_DELTA for a +1% move
  EQ_DELTA, EQ_GAMMA                             same on the equity spot
  EQ_VEGA   = V(vol+1pt) - V                     per +1 vol point
  IR_DV01   = V(curve+1bp) - V                   parallel, per currency
  IR_KRD    = V(node+1bp) - V                    per currency and curve tenor
"""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from .pricers_vec import price_trades
from .sim import SimCube

SPOT_BUMP = 0.01
VOL_BUMP = 0.01
IR_BUMP = 1e-4
ZERO_TOL = 1e-6          # sensitivities smaller than this (USD) are not stored
CHUNK = 400

OUT_COLUMNS = ["trade_id", "sensitivity_type", "risk_class", "risk_factor", "tenor_label", "value_usd"]


# ---------------------------------------------------------------------------
# Market state at t0
# ---------------------------------------------------------------------------
class MarketT0:
    """Valuation-date market taken from step 0 of a simulation cube."""

    def __init__(self, sim: SimCube):
        self.meta = copy.deepcopy(sim.meta)
        self.meta["dates"] = [self.meta["dates"][0]]
        self.as_of = sim.as_of
        self.ccys = list(sim.hw)
        self.fx = {c: float(a[0, 0]) for c, a in sim.fx.items() if c != "USD"}
        self.eq = {k: float(a[0, 0]) for k, a in sim.eq.items()}
        self.eq_ccy = dict(sim.eq_ccy)

    def cube(self, fx_mult=(1.0,), eq_mult=(1.0,), zeros=None, vol_shift: float = 0.0) -> SimCube:
        fx_mult, eq_mult = np.asarray(fx_mult, float), np.asarray(eq_mult, float)
        n = max(len(fx_mult), len(eq_mult))
        fx_mult, eq_mult = np.broadcast_to(fx_mult, n), np.broadcast_to(eq_mult, n)
        meta = dict(self.meta)
        meta["n_paths"] = n
        if zeros is not None:
            meta["zeros"] = zeros
        if vol_shift:
            meta["eq_vol"] = {k: {t: v + vol_shift for t, v in d.items()} for k, d in self.meta["eq_vol"].items()}
        x = {c: np.zeros((1, n)) for c in self.ccys}
        fx = {c: (s * fx_mult)[None, :] for c, s in self.fx.items()}
        eq = {k: (s * eq_mult)[None, :] for k, s in self.eq.items()}
        return SimCube(meta, np.zeros(1), x, fx, eq)

    def bumped_zeros(self, ccy: str, tenor: str | None) -> dict:
        """Zero curves with `ccy` bumped +1bp at one node (tenor) or everywhere (tenor None)."""
        z = copy.deepcopy(self.meta["zeros"])
        for lab in list(z[ccy]):
            if tenor is None or lab == tenor:
                z[ccy][lab] = z[ccy][lab] + IR_BUMP
        return z

    def snapshot(self) -> pd.DataFrame:
        """Market inputs used for pricing, keyed by the same risk_factor codes as the sensitivities."""
        rows = []
        for c, zs in self.meta["zeros"].items():
            for lab, v in zs.items():
                rows.append((f"IR_{c}", "IR", "ZERO_RATE", lab, float(v), "initial zero curve (continuous, ACT/365)"))
            a, s = self.meta["hw"][c]
            rows.append((f"IR_{c}", "IR", "HW_ALPHA", None, float(a), "Hull-White mean reversion"))
            rows.append((f"IR_{c}", "IR", "HW_SIGMA", None, float(s), "Hull-White volatility"))
        for c, s in self.fx.items():
            rows.append((f"FX_{c}", "FX", "FX_SPOT", None, s, f"USD per 1 {c}"))
        for k, s in self.eq.items():
            rows.append((f"EQ_{k}", "EQ", "EQ_SPOT", None, s, f"price in {self.eq_ccy.get(k, '')}"))
            for t, v in sorted(self.meta["eq_vol"].get(k, {}).items(), key=lambda kv: float(kv[0])):
                rows.append((f"EQ_{k}", "EQ", "EQ_VOL", f"{float(t):g}Y", float(v), "historical vol term point"))
            rows.append((f"EQ_{k}", "EQ", "DIV_YIELD", None, float(self.meta["div_yield"].get(k, 0.015)),
                         "continuous dividend yield"))
        return pd.DataFrame(rows, columns=["risk_factor", "risk_class", "input_type", "tenor_label", "value", "description"])


# ---------------------------------------------------------------------------
# Factor mapping
# ---------------------------------------------------------------------------
def fx_ccy(trades: pd.DataFrame, eq_ccy: dict) -> np.ndarray:
    """The one non-USD currency whose FX rate each trade's USD value depends on (None if USD only)."""
    p = trades["product"].to_numpy()
    c = trades["ccy"].astype(object).to_numpy()
    c2 = trades["ccy_2"].astype(object).to_numpy() if "ccy_2" in trades else np.full(len(trades), None)
    und = trades["underlying"].astype(object).to_numpy() if "underlying" in trades else np.full(len(trades), None)
    out = np.where(p == "xccy", c2, c)
    opt = np.isin(p, ["euro_opt", "amer_opt"])
    out = np.where(opt, [eq_ccy.get(u, cc) for u, cc in zip(und, c)], out)
    return np.where(out == "USD", None, out)


def curve_exposed(trades: pd.DataFrame, ccy: str, eq_ccy: dict) -> np.ndarray:
    p = trades["product"].to_numpy()
    c = trades["ccy"].astype(object).to_numpy()
    c2 = trades["ccy_2"].astype(object).to_numpy() if "ccy_2" in trades else np.full(len(trades), None)
    und = trades["underlying"].astype(object).to_numpy() if "underlying" in trades else np.full(len(trades), None)
    m = np.isin(p, ["irs", "fra"]) & (c == ccy)
    m |= (p == "xccy") & ((ccy == "USD") | (c2 == ccy))
    m |= (p == "fxfwd") & ((ccy == "USD") | (c == ccy))
    opt = np.isin(p, ["euro_opt", "amer_opt"])
    m |= opt & (np.array([eq_ccy.get(u, cc) for u, cc in zip(und, c)], dtype=object) == ccy)
    return m


def _price(trades: pd.DataFrame, cube: SimCube) -> np.ndarray:
    """(n, paths) USD values at t0, priced in chunks."""
    out = np.zeros((len(trades), cube.n_paths))
    for s in range(0, len(trades), CHUNK):
        part = trades.iloc[s:s + CHUNK].reset_index(drop=True)
        out[s:s + CHUNK] = np.nan_to_num(price_trades(part, cube, steps=[0])[:, 0, :])
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def compute(trades: pd.DataFrame, mkt: MarketT0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (mtm, sens). mtm: trade_id, mtm_usd. sens: OUT_COLUMNS (long format, non-zero values only)."""
    trades = trades.reset_index(drop=True)
    ids = trades["trade_id"].to_numpy()
    rows = []

    def emit(mask, stype, rclass, factors, tenor, vals):
        keep = mask & np.isfinite(vals) & (np.abs(vals) > ZERO_TOL)
        if keep.any():
            rows.append(pd.DataFrame({"trade_id": ids[keep], "sensitivity_type": stype, "risk_class": rclass,
                                      "risk_factor": np.asarray(factors, dtype=object)[keep] if not isinstance(factors, str) else factors,
                                      "tenor_label": tenor, "value_usd": vals[keep]}))

    # base + spot bumps on the path dimension
    V = _price(trades, mkt.cube(fx_mult=[1, 1 + SPOT_BUMP, 1 - SPOT_BUMP, 1, 1],
                                eq_mult=[1, 1, 1, 1 + SPOT_BUMP, 1 - SPOT_BUMP]))
    base = V[:, 0]
    fxc = fx_ccy(trades, mkt.eq_ccy)
    has_fx = np.array([c is not None for c in fxc])
    fx_f = np.array([f"FX_{c}" if c is not None else "" for c in fxc], dtype=object)
    emit(has_fx, "FX_DELTA", "FX", fx_f, None, (V[:, 1] - V[:, 2]) / 2)
    emit(has_fx, "FX_GAMMA", "FX", fx_f, None, V[:, 1] + V[:, 2] - 2 * base)

    is_opt = trades["product"].isin(["euro_opt", "amer_opt"]).to_numpy()
    if is_opt.any():
        und = trades["underlying"].astype(object).to_numpy()
        eq_f = np.array([f"EQ_{u}" for u in und], dtype=object)
        emit(is_opt, "EQ_DELTA", "EQ", eq_f, None, (V[:, 3] - V[:, 4]) / 2)
        emit(is_opt, "EQ_GAMMA", "EQ", eq_f, None, V[:, 3] + V[:, 4] - 2 * base)
        opt = trades[is_opt]
        vv = np.zeros(len(trades))
        vv[is_opt] = _price(opt, mkt.cube(vol_shift=VOL_BUMP))[:, 0]
        emit(is_opt, "EQ_VEGA", "EQ", eq_f, None, vv - base)

    # interest-rate curve bumps, per currency, exposed trades only
    for ccy in mkt.ccys:
        m = curve_exposed(trades, ccy, mkt.eq_ccy)
        if not m.any():
            continue
        sub = trades[m]
        b0 = base[m]
        full = np.zeros(len(trades))
        full[m] = _price(sub, mkt.cube(zeros=mkt.bumped_zeros(ccy, None)))[:, 0] - b0
        emit(m, "IR_DV01", "IR", f"IR_{ccy}", None, full)
        for tenor in mkt.meta["zeros"][ccy]:
            k = np.zeros(len(trades))
            k[m] = _price(sub, mkt.cube(zeros=mkt.bumped_zeros(ccy, tenor)))[:, 0] - b0
            emit(m, "IR_KRD", "IR", f"IR_{ccy}", tenor, k)

    mtm = pd.DataFrame({"trade_id": ids, "mtm_usd": base})
    sens = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUT_COLUMNS)
    return mtm, sens[OUT_COLUMNS]
