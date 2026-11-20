"""Extended Vasicek / Hull-White one-factor model: calibration and analytic bond prices.

Short rate r(t) = x(t) + phi(t),  dx = -a x dt + sigma dW,  x(0) = 0
phi(t) = f(0,t) + sigma^2 / (2 a^2) (1 - e^{-a t})^2   (fits today's discount curve exactly)

Zero-coupon bond on a path (Brigo-Mercurio):
  P(t,T) = P0(T)/P0(t) * exp{ 1/2 [V(t,T) - V(0,T) + V(0,t)] - B(t,T) x(t) }
  B(t,T) = (1 - e^{-a (T-t)}) / a
  V(t,T) = sigma^2/a^2 [ (T-t) + 2/a e^{-a(T-t)} - 1/(2a) e^{-2a(T-t)} - 3/(2a) ]

Calibration to historical discount-factor volatility (weekly):
  M1 / M10 = (1 - e^{-a}) / (1 - e^{-10 a})       (M = annualised vol of log DF, 1Y and 10Y)
  sigma    = M10 * a / (1 - e^{-10 a})
  bounds 0.1 < a < 1, 0 < sigma < 0.022; fallback a = 0.1 (floor), sigma = 0.022 (ceiling)
  Cases 1-7 implemented in ``calibrate``.
"""
from __future__ import annotations

import math

import numpy as np

ALPHA_MIN = 0.1
ALPHA_MAX = 1.0
SIGMA_MAX = 0.022
MIN_OBS = 252
MAX_ITER = 100
TOL = 1e-10


def vol_ratio(a: float) -> float:
    return (1.0 - math.exp(-a)) / (1.0 - math.exp(-10.0 * a))


def sigma_from_alpha(a: float, vol10: float) -> float:
    return vol10 * a / (1.0 - math.exp(-10.0 * a))


def _secant(target: float, x0: float = 0.2, x1: float = 0.5):
    f0, f1 = vol_ratio(x0) - target, vol_ratio(x1) - target
    for _ in range(MAX_ITER):
        if f1 == f0:
            return None
        x2 = x1 - f1 * (x1 - x0) / (f1 - f0)
        if not np.isfinite(x2) or x2 <= 0:
            x2 = max(1e-6, 0.5 * x1)
        x0, f0 = x1, f1
        x1, f1 = x2, vol_ratio(x2) - target
        if abs(f1) < TOL:
            return x1
    return None


def calibrate(vol1: float | None, vol10: float | None, n_obs: int,
              alpha_override: float | None = None, sigma_override: float | None = None) -> dict:
    """Calibrate (alpha, sigma) from annualised vols of log 1Y and 10Y discount factors."""
    def out(a, s, case, fallback):
        if alpha_override is not None:
            a = float(alpha_override)
        if sigma_override is not None:
            s = float(sigma_override)
        s = float(min(max(s, 1e-6), SIGMA_MAX))
        return dict(alpha=float(a), sigma=s, calibration_case=case, is_fallback=fallback,
                    vol_df1y=vol1, vol_df10y=vol10, vol_ratio=(vol1 / vol10) if vol1 and vol10 else None,
                    n_obs=int(n_obs))

    if n_obs is None or n_obs < MIN_OBS:
        return out(ALPHA_MIN, SIGMA_MAX, 3, True)                       # insufficient series
    if not vol1 or not vol10 or not np.isfinite(vol1) or not np.isfinite(vol10):
        return out(ALPHA_MIN, SIGMA_MAX, 1, True)                       # bond vol = 0 / missing
    ratio = vol1 / vol10
    if not (0.1 < ratio < 1.0):
        return out(ALPHA_MIN, sigma_from_alpha(ALPHA_MIN, vol10), 2, True)   # infeasible ratio
    a = _secant(ratio)
    if a is None:
        return out(ALPHA_MIN, SIGMA_MAX, 4, True)                       # no convergence
    if a < ALPHA_MIN:
        return out(ALPHA_MIN, sigma_from_alpha(ALPHA_MIN, vol10), 5, True)
    if a > ALPHA_MAX:
        return out(ALPHA_MIN, sigma_from_alpha(ALPHA_MIN, vol10), 6, True)  # alpha_min, not alpha_max
    return out(a, sigma_from_alpha(a, vol10), 7, False)


class HW1F:
    """Analytic Hull-White 1F on top of a ``curves.ZeroCurve`` initial curve."""

    def __init__(self, curve, alpha: float, sigma: float):
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

    def x_step(self, x, dt, z):
        """Exact transition of the OU state over dt with standard normals z."""
        a, s = self.a, self.s
        return x * math.exp(-a * dt) + s * math.sqrt((1.0 - math.exp(-2 * a * dt)) / (2 * a)) * z

    def log_A(self, t: float, T):
        """ln of the deterministic part of P(t,T); T array-like (> t)."""
        T = np.asarray(T, dtype=float)
        tau = np.maximum(T - t, 0.0)
        return (self.curve.log_df(T) - self.curve.log_df(t)
                + 0.5 * (self.V(tau) - self.V(T) + self.V(t)))

    def P(self, t: float, T, x):
        """Bond prices: T shape (...), x shape (P,) -> (..., P)."""
        T = np.asarray(T, dtype=float)
        lnA = self.log_A(t, T)
        B = self.B(np.maximum(T - t, 0.0))
        return np.exp(lnA[..., None] - B[..., None] * np.asarray(x)[(None,) * T.ndim])

    def zero_rates(self, t: float, taus, x):
        """Simulated continuously-compounded zero rates for maturities t+tau."""
        taus = np.asarray(taus, dtype=float)
        P = self.P(t, t + taus, x)
        return -np.log(P) / taus[:, None]
