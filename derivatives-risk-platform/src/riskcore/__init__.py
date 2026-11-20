"""riskcore: market simulation, pricing, exposure (PFE) and VaR for the derivatives risk platform.

Modules
-------
config      static reference data (currencies, tenors, series registry, equity universe)
timegrid    simulation date grid, weekly seed, Box-Muller normals
marketdata  readers for silver market tables (with bronze fallback)
ingest      FRED / yfinance / ECB / BoE fetchers and idempotent Delta upserts
curves      QuantLib curve bootstrap + numpy ZeroCurve
vol         historical volatility (log returns, EWMA, term structure)
corr        correlation matrix, PSD repair, SVD factorisation
hullwhite   Hull-White 1F calibration and analytic bond prices
sim         Monte Carlo scenario generation
tradegen    synthetic trade and reference data generator
pricers_vec vectorised grid pricers
pricers_ql  QuantLib pricers (t0 valuation, cross-check, app)
exposure    netting-set / counterparty exposure aggregation
var         book-level VaR statistics
evidence    text evidence writer
"""

__version__ = "0.1.0"
