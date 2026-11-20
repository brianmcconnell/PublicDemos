# Derivatives Risk Platform — Methodology

This document describes the quantitative methods used in the platform. It is written in neutral
terms for the public repository and does not contain any proprietary or confidential content.

## 1. Market Data

**Interest rates** — USD Treasury constant-maturity par yields (11 tenors, 1M to 30Y) from the
Federal Reserve via FRED. Non-USD curves (EUR, GBP, JPY, CHF, CAD) are built from overnight
rates plus OECD-published government bond yields at 3M and 10Y, forward-filled to daily frequency.
These are proxies; a production system would use swap curves.

**FX** — Daily spot rates from FRED (DEX series for major pairs) and Yahoo Finance, quoted as
USD per one unit of foreign currency.

**Equities** — Daily adjusted close prices from Yahoo Finance for six country indices and up to
40 single-name stocks across six currencies.

**Credit** — ICE BofA option-adjusted spreads from FRED (investment-grade and high-yield) plus
synthetic CDS-style spreads.

## 2. Yield Curve Construction

**USD** — QuantLib piecewise log-cubic discount bootstrap. Deposits (ACT/360) for tenors up to
1Y, fixed-rate bonds (semi-annual 30/360) for 2Y–30Y. A flat extrapolation node is appended at
50Y so that long-dated trades do not fail.

**Non-USD** — Log-linear interpolation of the available zero-rate points. Flat extrapolation
beyond the longest observed tenor.

## 3. Historical Volatility

Factor levels are constructed for each risk driver:

* IR: −ln DF(t) for the 1Y and 10Y discount factors (sign-flipped so a positive shock = higher
  rates).
* FX: ln(S_t) for each FX rate.
* EQ: ln(P_t) for each equity index and single name.

Daily log-returns are computed. Annualised volatility is measured using:

* Rolling windows of 63, 252, 756 and 2 520 business days.
* Exponentially-weighted moving average (EWMA) with λ = 0.94.

The four rolling windows map to nominal horizons of 3M, 1Y, 3Y and 10Y. The vol term structure
is interpolated linearly in total variance (vol² × T).

Forward (instantaneous) variance between simulation steps is backed out from the term structure,
floored at a configurable minimum (3% for FX, 5% for equity).

## 4. Correlation Matrix

Pairwise Pearson correlation on the most recent 504 business days (≈ 2 years) of daily log-returns
for the core factors (6 IR + 5 FX + 6–17 EQ indices). The IR factors are sign-flipped so that
the convention matches the simulation (positive shock = higher rates).

The raw matrix is repaired to positive semi-definite by eigenvalue clipping: any eigenvalue below
a floor ε = 10⁻⁶ is raised to ε, and the matrix is renormalised so the diagonal is 1.

SVD factorisation A such that AAᵀ = C is used to generate correlated normals in the simulation.

## 5. Hull-White One-Factor Model

The short rate follows an extended Vasicek (Hull-White 1F) process:

    r(t) = x(t) + φ(t)
    dx = −a x dt + σ dW,    x(0) = 0
    φ(t) = f(0,t) + σ²/(2a²) (1 − e^{−at})²

where f(0,t) is the instantaneous forward rate from today’s discount curve.

**Calibration** uses the ratio of annualised volatilities of the 1Y and 10Y log discount factors:

    vol(1Y) / vol(10Y) = (1 − e^{−a}) / (1 − e^{−10a})

The equation is solved by secant iteration. Once a is found:

    σ = vol(10Y) × a / (1 − e^{−10a})

Bounds: 0.1 ≤ a ≤ 1.0, σ ≤ 0.022. Seven calibration cases handle missing data, infeasible
ratios, non-convergence and boundary violations. Fallback parameters (a = 0.1, σ = 0.022)
are used when calibration is not possible.

**Analytic bond prices** on a simulated path (Brigo & Mercurio):

    P(t,T) = P(0,T)/P(0,t) × exp{ ½[V(t,T) − V(0,T) + V(0,t)] − B(t,T) x(t) }

where B(τ) = (1 − e^{−aτ})/a and V(τ) = σ²/a² [τ + 2/a e^{−aτ} − 1/(2a) e^{−2aτ} − 3/(2a)].

## 6. Monte Carlo Simulation

A 46-step time grid: today, 10 daily, 4 weekly, monthly to 2Y, annual to 10Y. The default is
1 000 paths (2 000 in production).

Correlated standard normals are generated via SVD of the correlation matrix. Each factor is
driven by a single Brownian driver per core factor; single-name equity returns are decomposed
into a correlated (index-driven) component and an idiosyncratic component via a beta parameter.

**Interest rates** — Exact OU transition: x(t+dt) = x(t) e^{−a dt} + σ √[(1 − e^{−2a dt})/(2a)] z.
Bond prices use the analytic formula.

**FX** — GBM with forward-variance from the vol term structure.

**Equities** — GBM with forward-variance; indices are core factors, single names are
driven by β × (index return) + √(1 − β²) × (idiosyncratic).

## 7. Pricing

All six products are valued on every (time step, path) by the vectorised grid pricer.
Conventions match ACT/365F, unadjusted schedules, NullCalendar.

| Product | Grid pricer | QuantLib cross-check |
| --- | --- | --- |
| IRS | Cash-flow discounting with HW bond prices | DiscountingSwapEngine + IborIndex |
| XCCY | Fixed/fixed legs with initial and final notional exchange | FixedRateLeg |
| FRA | N [P(t,Ts) − P(t,Te) − Kτ P(t,Te)] | VanillaSwap (single period) |
| FX Forward | N [S(t) P_ccy(t,T) − K P_USD(t,T)] | Direct formula |
| European Option | Black-Scholes | AnalyticEuropeanEngine |
| American Option | Barone-Adesi-Whaley (1987) | BaroneAdesiWhaleyApproximationEngine |

The QuantLib pricer runs on today’s market and is used as a t=0 cross-check (< 0.01 bp of
notional for all products).

## 8. Exposure Aggregation

For each counterparty and time step:

    E(t) = Σ_{netting sets} max(Σ_{trades in set} V(t), 0) + Σ_{NA trades} max(V(t), 0)

Trades without a netting agreement (≥ 1/3 of the portfolio) are floored at zero individually.
Netted trades are summed within the netting set and then floored.

* **EE(t)** = mean of E(t) over paths
* **PFE 95(t)** = 95th percentile of E(t) over paths

Exposures are uncollateralised (collateral agreements are stored as reference data but not
applied in the current version).

## 9. Book-Level VaR

For each book and time step, the simulated PV distribution gives:

* PnL = PV(t, path) − PV(t=0)
* VaR 95 = 95th percentile of the loss distribution = t0_pv − 5th percentile of PV
* Expected Shortfall 95 = mean of losses exceeding VaR 95

## 10. Load Balancing

Trades are grouped by netting key (netting set ID, or counterparty ID for un-netted trades).
Large keys are split into chunks of ~1 500 trades, and chunks are packed into approximately
equal-size bins using a greedy longest-first algorithm. Each bin is priced independently and
emits partial sums (linear for netted trades, already-floored for un-netted trades). A
finalization step adds the partials per counterparty, floors netting sets at zero, and
computes the distribution statistics.
