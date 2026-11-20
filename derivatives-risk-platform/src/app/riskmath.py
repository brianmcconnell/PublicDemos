"""Curve and Hull-White 1F maths for the app.

A self-contained copy of the formulas in riskcore.curves.ZeroCurve and riskcore.hullwhite.HW1F, so the app
rebuilds rates exactly as the simulation does (validated against market.gold_sim_curves):

  r(t)        = x(t) + phi(t),   phi(t) = f(0,t) + sigma^2/(2 a^2) (1 - e^{-a t})^2
  P(t,T | x)  = P0(T)/P0(t) exp{ 1/2 [V(T-t) - V(T) + V(t)] - B(T-t) x }
  zero(t,tau) = -ln P(t, t+tau | x) / tau
"""
from __future__ import annotations

import re

import numpy as np

EXTRAP_NODE = 50.0
TENOR_RE = re.compile(r"^([0-9]+)([MY])$")


def tenor_years(label: str) -> float:
    """'18M' -> 1.5, '25Y' -> 25.0. Raises ValueError for anything else."""
    m = TENOR_RE.match((label or "").strip().upper())
    if not m:
        raise ValueError("Tenor must be a whole number followed by M or Y, e.g. 18M or 25Y")
    n = int(m.group(1))
    if n <= 0:
        raise ValueError("Tenor must be greater than zero")
    return n / 12.0 if m.group(2) == "M" else float(n)


class ZeroCurve:
    """Linear-in-zero-rate curve on ACT/365 year fractions, flat at both ends (riskcore.curves.ZeroCurve)."""

    def __init__(self, times, zeros):
        t = np.round(np.asarray(times, dtype=float) * 365.0) / 365.0
        z = np.asarray(zeros, dtype=float)
        order = np.argsort(t)
        t, z = t[order], z[order]
        if t[0] > 0:
            t, z = np.concatenate([[0.0], t]), np.concatenate([[z[0]], z])
        if t[-1] < EXTRAP_NODE:
            t, z = np.concatenate([t, [EXTRAP_NODE]]), np.concatenate([z, [z[-1]]])
        self.times, self.zeros = t, z

    def zero(self, t):
        return np.interp(np.asarray(t, dtype=float), self.times, self.zeros)

    def log_df(self, t):
        t = np.asarray(t, dtype=float)
        return -self.zero(t) * t

    def fwd(self, t, h: float = 1e-4):
        t = np.asarray(t, dtype=float)
        lo, hi = np.maximum(t - h, 0.0), t + h
        return -(self.log_df(hi) - self.log_df(lo)) / (hi - lo)


class HW1F:
    """Analytic Hull-White 1F on an initial ZeroCurve (riskcore.hullwhite.HW1F)."""

    def __init__(self, curve: ZeroCurve, alpha: float, sigma: float):
        self.curve, self.a, self.s = curve, float(alpha), float(sigma)

    def B(self, tau):
        return (1.0 - np.exp(-self.a * tau)) / self.a

    def V(self, tau):
        a, s = self.a, self.s
        tau = np.asarray(tau, dtype=float)
        return (s * s) / (a * a) * (tau + 2.0 / a * np.exp(-a * tau) - 0.5 / a * np.exp(-2.0 * a * tau) - 1.5 / a)

    def phi(self, t):
        a, s = self.a, self.s
        t = np.asarray(t, dtype=float)
        return self.curve.fwd(t) + s * s / (2 * a * a) * (1.0 - np.exp(-a * t)) ** 2

    def zero_rate(self, t: float, tau: float, x: np.ndarray) -> np.ndarray:
        """Simulated zero rate for maturity t + tau on every path (x: shape (P,))."""
        T = t + tau
        log_a = (self.curve.log_df(T) - self.curve.log_df(t)
                 + 0.5 * (self.V(max(T - t, 0.0)) - self.V(T) + self.V(t)))
        log_p = log_a - self.B(max(T - t, 0.0)) * np.asarray(x, dtype=float)
        return -log_p / tau

    def short_rate(self, t: float, x: np.ndarray) -> np.ndarray:
        return np.asarray(x, dtype=float) + float(self.phi(t))


def simulated_rate(hw: HW1F, t_years: np.ndarray, x: np.ndarray, tau: float | None) -> np.ndarray:
    """Rates on every step and path. x: (S, P); tau None = short rate. Returns (S, P)."""
    out = np.empty_like(np.asarray(x, dtype=float))
    for k, t in enumerate(t_years):
        out[k] = hw.short_rate(float(t), x[k]) if tau is None else hw.zero_rate(float(t), float(tau), x[k])
    return out
