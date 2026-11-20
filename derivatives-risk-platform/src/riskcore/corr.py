"""Risk factor correlation matrix (weekly, 2Y window) with PSD repair and SVD factors.

Core simulation factors:
  IR_{ccy}  short rate, proxied by the 1Y discount factor. The correlation is computed on
            log returns of the 1Y discount factor and then negated against every other factor
            (rho(r, f) = -1 * rho(b, f)); IR-IR correlations are unchanged (two sign flips).
  FX_{ccy}  log returns of USD per ccy
  EQ_{idx}  log returns of the country equity index
Single-name equities are not core factors: they load on their country index with weight
S = corr(name, index) plus an independent idiosyncratic normal.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import CCYS, CORR_WINDOW_DAYS, COUNTRY_INDEX, FX_CCYS, SINGLE_NAMES
from .vol import returns


def core_factor_names() -> list[str]:
    return ([f"IR_{c}" for c in CCYS] + [f"FX_{c}" for c in FX_CCYS]
            + [f"EQ_{i}" for i in COUNTRY_INDEX.values()])


def _return_panel(levels: pd.DataFrame, rtype: dict) -> pd.DataFrame:
    r = returns(levels, rtype)
    cols = {}
    for c in CCYS:
        if f"IR_{c}_DF1Y" in r:
            cols[f"IR_{c}"] = r[f"IR_{c}_DF1Y"]
    for c in FX_CCYS:
        if f"FX_{c}" in r:
            cols[f"FX_{c}"] = r[f"FX_{c}"]
    for t in list(COUNTRY_INDEX.values()) + list(SINGLE_NAMES):
        if f"EQ_{t}" in r:
            cols[f"EQ_{t}"] = r[f"EQ_{t}"]
    return pd.DataFrame(cols)


def pearson(window: pd.DataFrame, min_frac: float = 0.8) -> pd.DataFrame:
    """rho_xy = (1/n) sum (x-mu_x)(y-mu_y) / (sigma_x sigma_y), pairwise complete obs."""
    return window.corr(method="pearson", min_periods=int(len(window) * min_frac))


def nearest_psd(C: np.ndarray, eps: float = 1e-10) -> np.ndarray:
    C = 0.5 * (C + C.T)
    w, V = np.linalg.eigh(C)
    w = np.clip(w, eps, None)
    C2 = (V * w) @ V.T
    d = np.sqrt(np.diag(C2))
    C2 = C2 / np.outer(d, d)
    np.fill_diagonal(C2, 1.0)
    return C2


def svd_loadings(C: np.ndarray) -> np.ndarray:
    """A such that A A^T = C (C = U S U^T, A = U sqrt(S))."""
    U, S, _ = np.linalg.svd(C)
    return U * np.sqrt(np.clip(S, 0.0, None))


def correlation_snapshot(levels: pd.DataFrame, rtype: dict, calc_date, window: int = CORR_WINDOW_DAYS):
    """Return (core correlation DataFrame [PSD repaired], long rows for gold_corr_matrix)."""
    panel = _return_panel(levels, rtype)
    calc_date = pd.Timestamp(calc_date)
    win = panel.loc[:calc_date].tail(window)
    raw = pearson(win)
    # methodology sign flip: discount factor -> short rate
    sign = pd.Series([-1.0 if c.startswith("IR_") else 1.0 for c in raw.columns], index=raw.columns)
    raw = raw.mul(sign, axis=0).mul(sign, axis=1)

    core = [c for c in core_factor_names() if c in raw.columns]
    C = raw.loc[core, core].to_numpy(dtype=float)
    C = np.where(np.isfinite(C), C, 0.0)
    np.fill_diagonal(C, 1.0)
    C_psd = nearest_psd(C)
    core_df = pd.DataFrame(C_psd, index=core, columns=core)

    rows = []
    n_obs = int(win.dropna(how="all").shape[0])
    for i in raw.index:
        for j in raw.columns:
            v = core_df.loc[i, j] if (i in core and j in core) else raw.loc[i, j]
            if pd.notna(v):
                rows.append(dict(calc_date=calc_date.date(), factor_i=i, factor_j=j, correlation=float(v),
                                 is_core=bool(i in core and j in core), n_obs=n_obs))
    return core_df, rows


def core_matrix_from_long(long: pd.DataFrame) -> pd.DataFrame:
    """Rebuild the (PSD) core matrix from gold_corr_matrix rows for one calc_date."""
    core = long[long["is_core"]]
    M = core.pivot_table(index="factor_i", columns="factor_j", values="correlation")
    names = [c for c in core_factor_names() if c in M.index]
    M = M.reindex(index=names, columns=names).fillna(0.0)
    np.fill_diagonal(M.values, 1.0)
    return M
