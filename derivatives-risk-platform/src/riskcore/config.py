"""Static configuration and reference data for the risk platform."""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Currencies / tenors
# ---------------------------------------------------------------------------
BASE_CCY = "USD"
CCYS = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD"]
FX_CCYS = [c for c in CCYS if c != BASE_CCY]          # FX quoted as USD per 1 unit of ccy

TENORS = {
    "1M": 1 / 12, "3M": 0.25, "6M": 0.5, "1Y": 1.0, "2Y": 2.0, "3Y": 3.0, "5Y": 5.0,
    "7Y": 7.0, "10Y": 10.0, "15Y": 15.0, "20Y": 20.0, "30Y": 30.0,
}
# Tenors not on the bootstrap grid: read off the curve rebuilt from the simulated HW1F state
OFF_GRID_TENORS = {"18M": 1.5, "4Y": 4.0, "6Y": 6.0, "8Y": 8.0, "9Y": 9.0, "12Y": 12.0, "25Y": 25.0}
# Every simulated zero-rate tenor (gold_sim_curves, ref_tenor / ref_rate), ordered by maturity
SIM_RATE_TENORS = dict(sorted({**TENORS, **OFF_GRID_TENORS}.items(), key=lambda kv: kv[1]))
SHORT_RATE_LABEL = "SHORT"     # tenor_label used for the HW1F short rate in gold_sim_curves
VOL_TERM_POINTS = {"vol_3m": 0.25, "vol_1y": 1.0, "vol_3y": 3.0, "vol_10y": 10.0}
VOL_WINDOWS = {"vol_3m": 63, "vol_1y": 252, "vol_3y": 756, "vol_10y": 2520}
EWMA_LAMBDA = 0.94
FX_VOL_FLOOR = 0.03
EQ_VOL_FLOOR = 0.05
DEFAULT_DIV_YIELD = 0.015
CORR_WINDOW_DAYS = 504          # 2 years of daily returns

FLOAT_INDEX = {"USD": "SOFR", "EUR": "ESTR", "GBP": "SONIA", "JPY": "TONA", "CHF": "SARON", "CAD": "CORRA"}
VALID_TRADE_CCYS = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD")

# ---------------------------------------------------------------------------
# FRED series registry: series_id -> (category, ccy, tenor_label, tenor_years, is_proxy)
#   category: CURVE_PAR (USD treasury CMT, bootstrapped), CURVE_ZERO (used as zero rate),
#             OVERNIGHT, FX, CREDIT, INFLATION, POLICY
#   tenor_years for OVERNIGHT = 1/365
# ---------------------------------------------------------------------------
ON = 1 / 365
FRED_SERIES = {
    # USD Treasury constant maturity (par yields, %)
    "DGS1MO": ("CURVE_PAR", "USD", "1M", 1 / 12, False),
    "DGS3MO": ("CURVE_PAR", "USD", "3M", 0.25, False),
    "DGS6MO": ("CURVE_PAR", "USD", "6M", 0.5, False),
    "DGS1": ("CURVE_PAR", "USD", "1Y", 1.0, False),
    "DGS2": ("CURVE_PAR", "USD", "2Y", 2.0, False),
    "DGS3": ("CURVE_PAR", "USD", "3Y", 3.0, False),
    "DGS5": ("CURVE_PAR", "USD", "5Y", 5.0, False),
    "DGS7": ("CURVE_PAR", "USD", "7Y", 7.0, False),
    "DGS10": ("CURVE_PAR", "USD", "10Y", 10.0, False),
    "DGS20": ("CURVE_PAR", "USD", "20Y", 20.0, False),
    "DGS30": ("CURVE_PAR", "USD", "30Y", 30.0, False),
    # Overnight rates
    "SOFR": ("OVERNIGHT", "USD", "ON", ON, False),
    "DFF": ("POLICY", "USD", "ON", ON, False),
    "ECBESTRVOLWGTTRMDMNRT": ("OVERNIGHT", "EUR", "ON", ON, False),
    "IUDSOIA": ("OVERNIGHT", "GBP", "ON", ON, False),
    # OECD monthly proxies (3M interbank, 10Y government), forward-filled to daily
    "IR3TIB01EZM156N": ("CURVE_ZERO", "EUR", "3M", 0.25, True),
    "IRLTLT01EZM156N": ("CURVE_ZERO", "EUR", "10Y", 10.0, True),
    "IR3TIB01GBM156N": ("CURVE_ZERO", "GBP", "3M", 0.25, True),
    "IRLTLT01GBM156N": ("CURVE_ZERO", "GBP", "10Y", 10.0, True),
    "IR3TIB01JPM156N": ("CURVE_ZERO", "JPY", "3M", 0.25, True),
    "IRLTLT01JPM156N": ("CURVE_ZERO", "JPY", "10Y", 10.0, True),
    "IR3TIB01CHM156N": ("CURVE_ZERO", "CHF", "3M", 0.25, True),
    "IRLTLT01CHM156N": ("CURVE_ZERO", "CHF", "10Y", 10.0, True),
    "IR3TIB01CAM156N": ("CURVE_ZERO", "CAD", "3M", 0.25, True),
    "IRLTLT01CAM156N": ("CURVE_ZERO", "CAD", "10Y", 10.0, True),
    # FX (FRED H.10)
    "DEXUSEU": ("FX", "EUR", None, None, False),   # USD per EUR
    "DEXUSUK": ("FX", "GBP", None, None, False),   # USD per GBP
    "DEXJPUS": ("FX", "JPY", None, None, False),   # JPY per USD (inverted)
    "DEXSZUS": ("FX", "CHF", None, None, False),   # CHF per USD (inverted)
    "DEXCAUS": ("FX", "CAD", None, None, False),   # CAD per USD (inverted)
    # Credit (ICE BofA OAS, %) -- FRED only publishes ~3 years of history
    "BAMLC0A1CAAA": ("CREDIT", "USD", "AAA", None, False),
    "BAMLC0A2CAA": ("CREDIT", "USD", "AA", None, False),
    "BAMLC0A3CA": ("CREDIT", "USD", "A", None, False),
    "BAMLC0A4CBBB": ("CREDIT", "USD", "BBB", None, False),
    "BAMLH0A1HYBB": ("CREDIT", "USD", "BB", None, False),
    "BAMLH0A2HYB": ("CREDIT", "USD", "B", None, False),
    "BAMLH0A3HYC": ("CREDIT", "USD", "CCC", None, False),
    # Inflation / real rates
    "DFII5": ("INFLATION", "USD", "5Y", 5.0, False),
    "DFII10": ("INFLATION", "USD", "10Y", 10.0, False),
    "T5YIE": ("INFLATION", "USD", "5Y", 5.0, False),
    "T10YIE": ("INFLATION", "USD", "10Y", 10.0, False),
}
FX_INVERTED = {"DEXJPUS", "DEXSZUS", "DEXCAUS"}

# ECB yield curve (euro area AAA government spot rates, continuously compounded, %)
ECB_YC_TENORS = {"3M": 0.25, "6M": 0.5, "1Y": 1.0, "2Y": 2.0, "3Y": 3.0, "5Y": 5.0,
                 "7Y": 7.0, "10Y": 10.0, "15Y": 15.0, "20Y": 20.0, "30Y": 30.0}
ECB_YC_KEY = "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_{tenor}"

# Bank of England nominal zero-coupon gilt yields (%)
BOE_SERIES = {"IUDSNZC": ("5Y", 5.0), "IUDMNZC": ("10Y", 10.0), "IUDLNZC": ("20Y", 20.0)}

# ---------------------------------------------------------------------------
# Equity universe: country index per currency + single names
# ---------------------------------------------------------------------------
COUNTRY_INDEX = {"USD": "^GSPC", "GBP": "^FTSE", "EUR": "^STOXX50E", "JPY": "^N225",
                 "CHF": "^SSMI", "CAD": "^GSPTSE"}
INDEX_CCY = {v: k for k, v in COUNTRY_INDEX.items()}
SINGLE_NAMES = {
    "AAPL": "USD", "MSFT": "USD", "AMZN": "USD", "GOOGL": "USD", "META": "USD", "NVDA": "USD",
    "JPM": "USD", "BAC": "USD", "GS": "USD", "MS": "USD", "C": "USD", "WFC": "USD",
    "XOM": "USD", "CVX": "USD", "JNJ": "USD", "PFE": "USD", "PG": "USD", "KO": "USD",
    "PEP": "USD", "WMT": "USD", "HD": "USD", "DIS": "USD", "INTC": "USD", "CSCO": "USD",
    "ORCL": "USD", "IBM": "USD", "CAT": "USD", "BA": "USD", "GE": "USD", "MMM": "USD",
    "HSBA.L": "GBP", "BP.L": "GBP", "SHEL.L": "GBP",
    "SAP.DE": "EUR", "SIE.DE": "EUR", "MC.PA": "EUR",
    "7203.T": "JPY", "6758.T": "JPY",
    "NESN.SW": "CHF", "NOVN.SW": "CHF",
    "RY.TO": "CAD", "TD.TO": "CAD",
}
EQUITY_CCY = {**SINGLE_NAMES, **INDEX_CCY}
ALL_TICKERS = list(COUNTRY_INDEX.values()) + list(SINGLE_NAMES.keys())

# London-listed prices from Yahoo are in pence (GBp) -> divide by 100
PENCE_TICKERS = {"HSBA.L", "BP.L", "SHEL.L"}


@dataclass
class Cfg:
    """Runtime configuration resolved from job/notebook parameters."""
    catalog: str = "serverless_stable_7s0j2u_catalog"
    schema: str = "market"

    def t(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"

    def vol(self, name: str) -> str:
        return f"/Volumes/{self.catalog}/{self.schema}/{name}"


def cfg_from_widgets(dbutils, defaults: dict | None = None) -> Cfg:
    """Create widgets (if missing) and build a Cfg from them."""
    defaults = defaults or {}
    for k, v in {"catalog": Cfg.catalog, "schema": Cfg.schema, **defaults}.items():
        try:
            dbutils.widgets.get(k)
        except Exception:
            dbutils.widgets.text(k, str(v))
    return Cfg(catalog=dbutils.widgets.get("catalog"), schema=dbutils.widgets.get("schema"))


def widget(dbutils, name: str, default: str) -> str:
    try:
        return dbutils.widgets.get(name)
    except Exception:
        dbutils.widgets.text(name, default)
        return dbutils.widgets.get(name)
