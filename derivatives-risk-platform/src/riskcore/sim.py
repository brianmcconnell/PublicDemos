"""Monte Carlo scenario generation for rates (HW1F), FX (GBM) and equities (lognormal).

All factors are simulated jointly on the methodology time grid with correlated normals:
  Z_core = N(0,1)^k  @ A^T,  A = U sqrt(S)  (SVD of the PSD core correlation matrix)
Rates:   x_{n+1} = x_n e^{-a dt} + sigma sqrt((1-e^{-2 a dt})/(2a)) Z_IR,  r = x + phi(t)
FX:      ln S_{n+1} = ln S_n + (r_USD - r_ccy - 1/2 s^2) dt + s sqrt(dt) Z_FX      (USD per ccy)
Equity:  ln S_{n+1} = ln S_n + (r_ccy - q - rho_eq,fx s_eq s_fx - 1/2 s^2) dt + s sqrt(dt) Z_EQ
         Z_EQ = S_eq,CI Z_CI + sqrt(1 - S_eq,CI^2) Z_ID  (single names; indices use Z_CI)
Instantaneous vols come from the historical vol term structure via forward variance,
floored (FX 3%, equity 5%). Seed = weekly integer date code; normals via polar Box-Muller.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CCYS, COUNTRY_INDEX, EQ_VOL_FLOOR, EQUITY_CCY, FX_CCYS, FX_VOL_FLOOR, TENORS
from .corr import core_factor_names, nearest_psd, svd_loadings
from .curves import ZeroCurve
from .hullwhite import HW1F
from .timegrid import box_muller_normals, simulation_dates, weekly_seed, year_fractions
from .vol import instantaneous_var


@dataclass
class MarketState:
    as_of: pd.Timestamp
    zeros: dict                      # ccy -> {tenor_label: zero}
    hw: dict                         # ccy -> (alpha, sigma)
    fx_spot: dict                    # ccy -> USD per ccy
    fx_vol: dict                     # ccy -> {tenor_years: vol}
    eq_spot: dict                    # ticker -> price (local ccy)
    eq_vol: dict                     # ticker -> {tenor_years: vol}
    eq_index_corr: dict = field(default_factory=dict)   # single name -> corr with its country index
    corr: pd.DataFrame | None = None                   # core correlation matrix
    div_yield: dict = field(default_factory=dict)


class SimCube:
    """Simulated scenarios. Arrays have shape (n_steps, n_paths)."""

    def __init__(self, meta: dict, t: np.ndarray, x: dict, fx: dict, eq: dict):
        self.meta, self.t, self.x, self.fx, self.eq = meta, t, x, fx, eq
        self.as_of = pd.Timestamp(meta["as_of"])
        self.dates = [pd.Timestamp(d) for d in meta["dates"]]
        self.n_steps, self.n_paths = len(t), meta["n_paths"]
        self.curves = {c: ZeroCurve.from_tenors(meta["zeros"][c]) for c in meta["zeros"]}
        self.hw = {c: HW1F(self.curves[c], *meta["hw"][c]) for c in meta["hw"]}
        self.fx.setdefault("USD", np.ones((self.n_steps, self.n_paths)))
        self.eq_vol = {k: {float(t_): v for t_, v in d.items()} for k, d in meta["eq_vol"].items()}
        self.div_yield = meta["div_yield"]
        self.eq_ccy = meta["eq_ccy"]

    # ---- persistence -----------------------------------------------------
    def save(self, path: str):
        arrays = {"t": self.t}
        for c, a in self.x.items():
            arrays[f"x__{c}"] = a.astype(np.float32)
        for c, a in self.fx.items():
            if c != "USD":
                arrays[f"fx__{c}"] = a.astype(np.float32)
        for k, a in self.eq.items():
            arrays[f"eq__{k}"] = a.astype(np.float32)
        arrays["meta"] = np.frombuffer(json.dumps(self.meta).encode(), dtype=np.uint8)
        with open(path, "wb") as fh:
            np.savez(fh, **arrays)

    @classmethod
    def load(cls, path: str) -> "SimCube":
        with np.load(path) as z:
            meta = json.loads(bytes(z["meta"]).decode())
            x = {k.split("__", 1)[1]: z[k].astype(np.float64) for k in z.files if k.startswith("x__")}
            fx = {k.split("__", 1)[1]: z[k].astype(np.float64) for k in z.files if k.startswith("fx__")}
            eq = {k.split("__", 1)[1]: z[k].astype(np.float64) for k in z.files if k.startswith("eq__")}
            t = z["t"].astype(np.float64)
        return cls(meta, t, x, fx, eq)

    def short_rate(self, ccy: str, step: int) -> np.ndarray:
        return self.x[ccy][step] + float(self.hw[ccy].phi(self.t[step]))


def _corr_loadings(corr: pd.DataFrame | None, names: list[str]) -> np.ndarray:
    k = len(names)
    C = np.eye(k)
    if corr is not None and len(corr):
        for i, a in enumerate(names):
            for j, b in enumerate(names):
                if a in corr.index and b in corr.columns and i != j:
                    v = corr.loc[a, b]
                    C[i, j] = v if np.isfinite(v) else 0.0
    return svd_loadings(nearest_psd(C)), C


def simulate(ms: MarketState, n_paths: int = 1000, seed: int | None = None) -> SimCube:
    as_of = pd.Timestamp(ms.as_of).normalize()
    dates = simulation_dates(as_of)
    t = year_fractions(dates, as_of)
    seed = weekly_seed(as_of) if seed is None else int(seed)
    rng = np.random.default_rng(seed)

    ccys = [c for c in CCYS if c in ms.zeros]
    fx_ccys = [c for c in FX_CCYS if c in ms.fx_spot and c in ccys]
    indices = [i for i in COUNTRY_INDEX.values() if i in ms.eq_spot]
    singles = [k for k in ms.eq_spot if k not in indices]
    names = core_factor_names()
    names = [n for n in names if (n.startswith("IR_") and n[3:] in ccys)
             or (n.startswith("FX_") and n[3:] in fx_ccys)
             or (n.startswith("EQ_") and n[3:] in indices)]
    pos = {n: i for i, n in enumerate(names)}
    A, C = _corr_loadings(ms.corr, names)

    curves = {c: ZeroCurve.from_tenors(ms.zeros[c]) for c in ccys}
    hw = {c: HW1F(curves[c], *ms.hw.get(c, (0.1, 0.01))) for c in ccys}
    S, P = len(t), int(n_paths)
    x = {c: np.zeros((S, P)) for c in ccys}
    fx = {c: np.zeros((S, P)) for c in fx_ccys}
    eq = {k: np.zeros((S, P)) for k in indices + singles}
    for c in fx_ccys:
        fx[c][0] = ms.fx_spot[c]
    for k in eq:
        eq[k][0] = ms.eq_spot[k]

    div = {k: float(ms.div_yield.get(k, 0.015)) for k in eq}
    fx_term = {c: ms.fx_vol.get(c, {0.25: 0.1, 10.0: 0.1}) for c in fx_ccys}
    eq_term = {k: ms.eq_vol.get(k, {0.25: 0.25, 10.0: 0.25}) for k in eq}
    idx_of = {k: COUNTRY_INDEX.get(EQUITY_CCY.get(k, "USD"), "^GSPC") for k in singles}

    for n in range(S - 1):
        dt = t[n + 1] - t[n]
        z = box_muller_normals(rng, P * len(names)).reshape(P, len(names)) @ A.T   # (P, k)
        r = {c: x[c][n] + float(hw[c].phi(t[n])) for c in ccys}
        for c in ccys:
            x[c][n + 1] = hw[c].x_step(x[c][n], dt, z[:, pos[f"IR_{c}"]])
        sfx = {}
        for c in fx_ccys:
            v = math.sqrt(instantaneous_var(fx_term[c], t[n], t[n + 1], FX_VOL_FLOOR))
            sfx[c] = v
            drift = (r["USD"] - r[c] - 0.5 * v * v) * dt
            fx[c][n + 1] = fx[c][n] * np.exp(drift + v * math.sqrt(dt) * z[:, pos[f"FX_{c}"]])
        z_idx = {}
        for k in indices:
            z_idx[k] = z[:, pos[f"EQ_{k}"]]
        z_id = box_muller_normals(rng, P * max(len(singles), 1)).reshape(max(len(singles), 1), P)
        for j, k in enumerate(indices + singles):
            ccy = EQUITY_CCY.get(k, "USD")
            v = math.sqrt(instantaneous_var(eq_term[k], t[n], t[n + 1], EQ_VOL_FLOOR))
            if k in indices:
                ze = z_idx[k]
                rho_fx = C[pos[f"EQ_{k}"], pos[f"FX_{ccy}"]] if ccy in sfx else 0.0
            else:
                ix = idx_of[k]
                s_ci = float(np.clip(ms.eq_index_corr.get(k, 0.6), -0.999, 0.999))
                base = z_idx.get(ix, np.zeros(P))
                ze = s_ci * base + math.sqrt(1 - s_ci * s_ci) * z_id[singles.index(k)]
                rho_fx = s_ci * C[pos[f"EQ_{ix}"], pos[f"FX_{ccy}"]] if (ccy in sfx and ix in z_idx) else 0.0
            quanto = rho_fx * v * sfx.get(ccy, 0.0)
            rc = r.get(ccy, r["USD"])
            drift = (rc - div[k] - quanto - 0.5 * v * v) * dt
            eq[k][n + 1] = eq[k][n] * np.exp(drift + v * math.sqrt(dt) * ze)

    meta = dict(as_of=str(as_of.date()), dates=[str(d.date()) for d in dates], n_paths=P, seed=seed,
                zeros={c: {k: float(v) for k, v in ms.zeros[c].items()} for c in ccys},
                hw={c: [float(hw[c].a), float(hw[c].s)] for c in ccys},
                eq_vol={k: {str(tt): float(vv) for tt, vv in eq_term[k].items()} for k in eq},
                fx_vol={c: {str(tt): float(vv) for tt, vv in fx_term[c].items()} for c in fx_ccys},
                div_yield=div, eq_ccy={k: EQUITY_CCY.get(k, "USD") for k in eq}, factors=names)
    return SimCube(meta, t, x, fx, eq)


# ---------------------------------------------------------------------------
# Tabular outputs
# ---------------------------------------------------------------------------
def curve_paths_long(cube: SimCube, tenors=None, paths=None) -> pd.DataFrame:
    tenors = tenors or ["3M", "1Y", "2Y", "5Y", "10Y", "30Y"]
    taus = np.array([TENORS[k] for k in tenors])
    pidx = np.arange(cube.n_paths) if paths is None else np.asarray(paths)
    frames = []
    for c, model in cube.hw.items():
        for s in range(cube.n_steps):
            z = model.zero_rates(cube.t[s], taus, cube.x[c][s][pidx])          # (n_tenor, P)
            df_ = np.exp(-z * taus[:, None])
            frames.append(pd.DataFrame({
                "time_step": s, "sim_date": cube.dates[s].date(), "t_years": float(cube.t[s]), "ccy": c,
                "tenor": np.repeat(tenors, len(pidx)), "path_id": np.tile(pidx, len(tenors)),
                "zero_rate": z.ravel(), "discount_factor": df_.ravel()}))
    return pd.concat(frames, ignore_index=True)


def series_paths_long(cube: SimCube, kind: str, paths=None) -> pd.DataFrame:
    src = cube.fx if kind == "fx" else cube.eq
    pidx = np.arange(cube.n_paths) if paths is None else np.asarray(paths)
    key = "ccy_pair" if kind == "fx" else "ticker"
    val = "fx_rate" if kind == "fx" else "price"
    frames = []
    for k, a in src.items():
        if kind == "fx" and k == "USD":
            continue
        name = f"{k}USD" if kind == "fx" else k
        sub = a[:, pidx]
        frames.append(pd.DataFrame({
            "time_step": np.repeat(np.arange(cube.n_steps), len(pidx)),
            "sim_date": np.repeat([d.date() for d in cube.dates], len(pidx)),
            "t_years": np.repeat(cube.t, len(pidx)), key: name,
            "path_id": np.tile(pidx, cube.n_steps), val: sub.ravel()}))
    return pd.concat(frames, ignore_index=True)


def percentile_summary(df: pd.DataFrame, keys: list[str], value: str) -> pd.DataFrame:
    g = df.groupby(keys)[value]
    out = g.quantile([0.05, 0.25, 0.5, 0.75, 0.95]).unstack()
    out.columns = ["p5", "p25", "p50", "p75", "p95"]
    out["mean"] = g.mean()
    return out.reset_index()
