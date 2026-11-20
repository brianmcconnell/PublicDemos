"""Book-level VaR from simulated book values.

For each book and time step the simulated book value distribution across paths gives
  mean_pv, p5_pv, p95_pv            (as requested: mean, 5th and 95th percentile)
  pnl = PV(t, path) - PV(t0)        (t0 value is deterministic)
  var_95 = 95th percentile of the loss distribution = -(5th percentile of pnl) = t0_pv - p5_pv
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def book_var(book_id: str, X: np.ndarray, t0_pv: float | None = None) -> pd.DataFrame:
    """X (steps, paths) simulated book values in USD."""
    t0 = float(X[0].mean()) if t0_pv is None else float(t0_pv)
    p5, p95 = np.percentile(X, [5, 95], axis=1)
    loss = t0 - X
    return pd.DataFrame({
        "book_id": book_id, "time_step": np.arange(X.shape[0]), "t0_pv": t0,
        "mean_pv": X.mean(axis=1), "p5_pv": p5, "p95_pv": p95,
        "var_95": np.percentile(loss, 95, axis=1),
        "expected_shortfall_95": np.array([row[row >= np.percentile(row, 95)].mean() for row in loss]),
    })


def var_from_long(pdf: pd.DataFrame) -> pd.DataFrame:
    """pdf columns: book_id, time_step, path_id, pv (one group = one book)."""
    b = pdf["book_id"].iloc[0]
    X = pdf.pivot_table(index="time_step", columns="path_id", values="pv", aggfunc="sum").sort_index().to_numpy()
    return book_var(b, X)
