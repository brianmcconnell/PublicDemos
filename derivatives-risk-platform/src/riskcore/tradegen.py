"""Synthetic trade and reference data generator.

Products (>= 50,000 each by default): IRS, XCCY, FRA, FX forward, European and American equity
options. Every trade carries book_id, counterparty_id, netting_set_id and collateral_id.

Distribution rules
* books ~ Zipf(0.5) over 25 books   -> the top 10 books each hold several % of trades
* counterparties ~ Zipf(1.1) over 300 -> CP0001 holds a very large trade set
* netting set ids are unique to a counterparty (NS-<cpty>-<k>); >= 1/3 of trades are 'NA'
* collateral agreements hang off netting sets (CSA-<ns>) or 'NA'; >= 1/3 of trades are 'NA'
* >= 500 trades (100 per product by default) carry deliberate data defects for quarantine
Fixed rates / strikes / forwards are set near today's market so PVs are realistic.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import COUNTRY_INDEX, EQUITY_CCY, FLOAT_INDEX, FX_CCYS
from .curves import ZeroCurve
from .timegrid import add_months

PRODUCTS = ["irs", "xccy", "fra", "fxfwd", "euro_opt", "amer_opt"]
PREFIX = {"irs": "IRS", "xccy": "XCY", "fra": "FRA", "fxfwd": "FXF", "euro_opt": "EOP", "amer_opt": "AOP"}
FILE_FORMAT = {"irs": "parquet", "xccy": "csv", "fra": "csv", "fxfwd": "parquet",
               "euro_opt": "xml", "amer_opt": "xml"}
CCY_W = {"USD": 0.40, "EUR": 0.25, "GBP": 0.15, "JPY": 0.10, "CHF": 0.05, "CAD": 0.05}
MIN_NA_SHARE = 0.34

_DESKS = ["Rates Flow", "Rates Exotics", "FX Spot & Fwd", "FX Options", "Equity Derivs", "XVA", "Treasury"]
_BU = ["Markets", "Corporate Banking", "Treasury"]
_SECTORS = ["Financials", "Energy", "Industrials", "Technology", "Healthcare", "Utilities",
            "Sovereign", "Consumer", "Real Estate", "Funds"]
_COUNTRIES = ["US", "GB", "DE", "FR", "JP", "CH", "CA", "NL", "SG", "AU"]
_RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC"]
_RATING_P = [0.04, 0.12, 0.30, 0.32, 0.12, 0.07, 0.03]
_NAME_A = ["Northgate", "Bluewater", "Silverline", "Redwood", "Ironbridge", "Highfield", "Oakmont",
           "Clearview", "Stonebrook", "Westmere", "Eastpoint", "Brightwell", "Kingsway", "Larkspur"]
_NAME_B = ["Capital", "Holdings", "Partners", "Industries", "Asset Management", "Energy", "Pension Fund",
           "Insurance", "Logistics", "Utilities", "Trading", "Group"]


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
def _zipf(n: int, s: float) -> np.ndarray:
    w = 1.0 / np.arange(1, n + 1) ** s
    return w / w.sum()


def reference_data(seed: int = 7, n_books: int = 25, n_cpty: int = 300, p_no_netting: float = 0.4,
                   p_csa: float = 0.5) -> dict:
    rng = np.random.default_rng(seed)
    books = pd.DataFrame({
        "book_id": [f"BK{i:03d}" for i in range(1, n_books + 1)],
        "desk": rng.choice(_DESKS, n_books),
        "business_unit": rng.choice(_BU, n_books, p=[0.7, 0.2, 0.1]),
        "book_weight": _zipf(n_books, 0.5),
    })
    cp_ids = [f"CP{i:04d}" for i in range(1, n_cpty + 1)]
    cpty = pd.DataFrame({
        "counterparty_id": cp_ids,
        "name": [f"{rng.choice(_NAME_A)} {rng.choice(_NAME_B)} {i:04d}" for i in range(1, n_cpty + 1)],
        "rating": rng.choice(_RATINGS, n_cpty, p=_RATING_P),
        "sector": rng.choice(_SECTORS, n_cpty),
        "country": rng.choice(_COUNTRIES, n_cpty),
        "cpty_weight": _zipf(n_cpty, 1.1),
    })
    has_agreement = rng.random(n_cpty) >= p_no_netting
    has_agreement[0] = True                       # the very large counterparty is netted
    cpty["has_netting_agreement"] = has_agreement
    ns_rows, csa_rows = [], []
    for cp, has in zip(cp_ids, has_agreement):
        if not has:
            continue
        for k in range(1, int(rng.integers(1, 4)) + 1):
            ns = f"NS-{cp}-{k}"
            ns_rows.append(dict(netting_set_id=ns, counterparty_id=cp, agreement_type="ISDA",
                                governing_law=rng.choice(["English", "New York"]),
                                signed_date=pd.Timestamp("2010-01-01") + pd.Timedelta(days=int(rng.integers(0, 4500)))))
            if rng.random() < p_csa:
                csa_rows.append(dict(collateral_id=f"CSA-{ns}", netting_set_id=ns, counterparty_id=cp,
                                     agreement_status="CSA", csa_ccy="USD",
                                     threshold=float(rng.choice([0, 1e6, 5e6, 10e6, 25e6])),
                                     mta=float(rng.choice([1e5, 2.5e5, 5e5, 1e6])), mpor_days=10))
    netting = pd.DataFrame(ns_rows)
    netting["signed_date"] = pd.to_datetime(netting["signed_date"]).dt.date
    collateral = pd.DataFrame(csa_rows)
    return dict(books=books, counterparties=cpty, netting=netting, collateral=collateral)


def _assign_common(n: int, rng, ref: dict) -> pd.DataFrame:
    books, cpty, netting, coll = ref["books"], ref["counterparties"], ref["netting"], ref["collateral"]
    book = rng.choice(books["book_id"].to_numpy(), n, p=books["book_weight"].to_numpy())
    cp = rng.choice(cpty["counterparty_id"].to_numpy(), n, p=cpty["cpty_weight"].to_numpy())
    sets = netting.groupby("counterparty_id")["netting_set_id"].apply(list).to_dict()
    u = rng.random(n)
    ns = np.array(["NA"] * n, dtype=object)
    for i in range(n):
        s = sets.get(cp[i])
        if s and u[i] >= 0.15:                    # 15% of netted-cpty trades sit outside the ISDA
            ns[i] = s[int(u[i] * 1000) % len(s)]
    na_share = float((ns == "NA").mean())
    if na_share < MIN_NA_SHARE:                   # top up to guarantee >= 1/3 'NA'
        idx = np.flatnonzero(ns != "NA")
        k = int(np.ceil((MIN_NA_SHARE - na_share) * n))
        ns[rng.choice(idx, k, replace=False)] = "NA"
    csa = dict(zip(coll["netting_set_id"], coll["collateral_id"])) if len(coll) else {}
    col = np.array([csa.get(x, "NA") if x != "NA" else "NA" for x in ns], dtype=object)
    return pd.DataFrame({"book_id": book, "counterparty_id": cp, "netting_set_id": ns, "collateral_id": col})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _d(as_of) -> np.datetime64:
    return np.datetime64(pd.Timestamp(as_of).date(), "D")


def _start_dates(as_of, month_offsets, rng) -> np.ndarray:
    base = _d(as_of).astype("datetime64[M]") + np.asarray(month_offsets).astype(int)
    return base.astype("datetime64[D]") + rng.integers(0, 28, len(month_offsets)).astype("timedelta64[D]")


def _yf(dates, as_of) -> np.ndarray:
    return (np.asarray(dates).astype("datetime64[D]") - _d(as_of)).astype(float) / 365.0


def _notional(rng, n, median=3e6, sigma=1.0):
    return np.clip(np.round(rng.lognormal(np.log(median), sigma, n), -3), 1e5, 5e8)


def _par_rate(curve: ZeroCurve, t_start, t_end, freq_m) -> np.ndarray:
    """Approximate par rate of the remaining schedule using today's curve."""
    t_start = np.maximum(np.asarray(t_start, float), 0.0)
    t_end = np.asarray(t_end, float)
    out = np.empty_like(t_end)
    for i in range(t_end.size):
        step = freq_m[i] / 12.0
        pts = np.arange(t_end[i], t_start[i], -step)[::-1]
        if pts.size == 0:
            pts = np.array([t_end[i]])
        prev = np.concatenate([[t_start[i]], pts[:-1]])
        ann = np.sum((pts - prev) * curve.df(pts))
        out[i] = (curve.df(t_start[i]) - curve.df(t_end[i])) / max(ann, 1e-9)
    return out


def _choice_ccy(rng, n):
    return rng.choice(list(CCY_W), n, p=list(CCY_W.values()))


def _ids(product, start, n):
    return [f"{PREFIX[product]}-{i:07d}" for i in range(start, start + n)]


def _base(product, n, start_id, as_of, rng, ref):
    df = _assign_common(n, rng, ref)
    df.insert(0, "trade_id", _ids(product, start_id, n))
    df.insert(1, "product", product)
    df["version"] = 1
    df["as_of_date"] = pd.Timestamp(as_of).date()
    return df


# ---------------------------------------------------------------------------
# Product generators
# ---------------------------------------------------------------------------
def gen_irs(n, as_of, curves, rng, ref, start_id=1):
    df = _base("irs", n, start_id, as_of, rng, ref)
    tenor = rng.choice([1, 2, 3, 5, 7, 10], n, p=[0.10, 0.15, 0.15, 0.25, 0.15, 0.20])
    seasoned = rng.random(n) < 0.7
    off = np.where(seasoned, -np.maximum(1, (rng.random(n) * tenor * 12 * 0.8).astype(int)),
                   rng.integers(1, 7, n))
    start = _start_dates(as_of, off, rng)
    mat = add_months(start, tenor * 12)
    freq = rng.choice([3, 6, 12], n, p=[0.4, 0.4, 0.2])
    ccy = _choice_ccy(rng, n)
    fixed = np.empty(n)
    last_reset = np.full(n, np.datetime64("NaT"), dtype="datetime64[D]")
    fixing = np.full(n, np.nan)
    asd = _d(as_of)
    for c in np.unique(ccy):
        m = ccy == c
        fixed[m] = _par_rate(curves[c], _yf(start[m], as_of), _yf(mat[m], as_of), freq[m])
    for k in range(1, 41):                      # current period for seasoned trades
        p_start = add_months(start, (k - 1) * freq)
        p_end = add_months(start, k * freq)
        cur = (p_start <= asd) & (p_end > asd)
        last_reset[cur] = p_start[cur]
        for c in np.unique(ccy[cur]):
            m = cur & (ccy == c)
            fixing[m] = curves[c].zero(_yf(p_end[m], as_of))
    fixed = fixed + rng.normal(0, 0.0025, n)
    fixing = fixing + rng.normal(0, 0.0005, n)
    df["ccy"] = ccy
    df["notional"] = _notional(rng, n)
    df["fixed_rate"] = np.round(fixed, 6)
    df["float_index"] = [FLOAT_INDEX[c] for c in ccy]
    df["pay_freq_months"] = freq
    df["start_date"] = pd.to_datetime(start).date
    df["maturity_date"] = pd.to_datetime(mat).date
    df["last_reset_date"] = pd.to_datetime(last_reset).date
    df["current_fixing"] = np.round(fixing, 6)
    df["direction"] = rng.choice(["PAY", "RECEIVE"], n)
    df["trade_date"] = pd.to_datetime(np.minimum(start, asd)).date
    df["last_reset_date"] = df["last_reset_date"].where(pd.notna(pd.to_datetime(last_reset)), None)
    return df


def gen_fra(n, as_of, curves, rng, ref, start_id=1):
    df = _base("fra", n, start_id, as_of, rng, ref)
    off = rng.integers(1, 24, n)
    start = _start_dates(as_of, off, rng)
    period = rng.choice([3, 6], n)
    end = add_months(start, period)
    ccy = _choice_ccy(rng, n)
    fwd = np.empty(n)
    for c in np.unique(ccy):
        m = ccy == c
        ts, te = _yf(start[m], as_of), _yf(end[m], as_of)
        fwd[m] = (curves[c].df(ts) / curves[c].df(te) - 1.0) / (te - ts)
    df["ccy"] = ccy
    df["notional"] = _notional(rng, n, median=10e6, sigma=0.8)
    df["fixed_rate"] = np.round(fwd + rng.normal(0, 0.0015, n), 6)
    df["float_index"] = [FLOAT_INDEX[c] for c in ccy]
    df["start_date"] = pd.to_datetime(start).date
    df["end_date"] = pd.to_datetime(end).date
    df["maturity_date"] = df["end_date"]
    df["settlement_date"] = df["start_date"]
    df["direction"] = rng.choice(["BUY", "SELL"], n)
    df["trade_date"] = (pd.Timestamp(as_of) - pd.to_timedelta(rng.integers(0, 60, n), unit="D")).date
    return df


def gen_xccy(n, as_of, curves, fx_spot, rng, ref, start_id=1):
    df = _base("xccy", n, start_id, as_of, rng, ref)
    tenor = rng.choice([2, 3, 5, 7, 10], n, p=[0.15, 0.2, 0.3, 0.15, 0.2])
    seasoned = rng.random(n) < 0.6
    off = np.where(seasoned, -np.maximum(1, (rng.random(n) * tenor * 12 * 0.8).astype(int)),
                   rng.integers(1, 7, n))
    start = _start_dates(as_of, off, rng)
    mat = add_months(start, tenor * 12)
    freq = rng.choice([6, 12], n)
    ccy2 = rng.choice(FX_CCYS, n, p=[0.4, 0.25, 0.15, 0.1, 0.1])
    notional = _notional(rng, n, median=20e6, sigma=0.8)
    fx0 = np.array([fx_spot[c] for c in ccy2]) * np.exp(rng.normal(0, 0.08, n))
    k1, k2 = np.empty(n), np.empty(n)
    k1[:] = _par_rate(curves["USD"], _yf(start, as_of), _yf(mat, as_of), freq)
    for c in np.unique(ccy2):
        m = ccy2 == c
        k2[m] = _par_rate(curves[c], _yf(start[m], as_of), _yf(mat[m], as_of), freq[m])
    df["ccy"] = "USD"
    df["ccy_2"] = ccy2
    df["notional"] = notional
    df["notional_2"] = np.round(notional / fx0, 2)
    df["fx_rate_at_inception"] = np.round(fx0, 6)
    df["fixed_rate"] = np.round(k1 + rng.normal(0, 0.002, n), 6)
    df["fixed_rate_2"] = np.round(k2 + rng.normal(0, 0.002, n), 6)
    df["float_index"] = "SOFR"
    df["pay_freq_months"] = freq
    df["start_date"] = pd.to_datetime(start).date
    df["maturity_date"] = pd.to_datetime(mat).date
    df["direction"] = rng.choice(["RECEIVE_CCY2", "PAY_CCY2"], n)
    df["trade_date"] = pd.to_datetime(np.minimum(start, _d(as_of))).date
    return df


def gen_fxfwd(n, as_of, curves, fx_spot, rng, ref, start_id=1):
    df = _base("fxfwd", n, start_id, as_of, rng, ref)
    base = rng.choice(FX_CCYS, n, p=[0.35, 0.25, 0.2, 0.1, 0.1])
    mat = _start_dates(as_of, rng.integers(1, 37, n), rng)
    T = _yf(mat, as_of)
    fwd = np.empty(n)
    for c in np.unique(base):
        m = base == c
        fwd[m] = fx_spot[c] * curves[c].df(T[m]) / curves["USD"].df(T[m])
    df["ccy_pair"] = [f"{c}USD" for c in base]
    df["ccy"] = base
    df["quote_ccy"] = "USD"
    df["notional"] = _notional(rng, n, median=5e6, sigma=0.9)
    df["forward_rate"] = np.round(fwd * np.exp(rng.normal(0, 0.03, n)), 6)
    df["trade_date"] = (pd.Timestamp(as_of) - pd.to_timedelta(rng.integers(0, 180, n), unit="D")).date
    df["start_date"] = df["trade_date"]
    df["maturity_date"] = pd.to_datetime(mat).date
    df["direction"] = rng.choice(["BUY", "SELL"], n)
    return df


def gen_option(product, n, as_of, eq_spot, rng, ref, start_id=1):
    df = _base(product, n, start_id, as_of, rng, ref)
    tickers = np.array(sorted(eq_spot))
    w = np.array([3.0 if t in COUNTRY_INDEX.values() else 1.0 for t in tickers])
    und = rng.choice(tickers, n, p=w / w.sum())
    spot = np.array([eq_spot[t] for t in und])
    strike = np.round(spot * np.exp(rng.normal(0, 0.15, n)), 4)
    mat = _start_dates(as_of, rng.integers(1, 37, n), rng)
    notional_ccy = _notional(rng, n, median=2e6, sigma=0.9)
    qty = np.maximum(1, np.round(notional_ccy / strike))
    df["underlying"] = und
    df["ccy"] = [EQUITY_CCY.get(t, "USD") for t in und]
    df["strike"] = strike
    df["quantity"] = qty
    df["notional"] = np.round(qty * strike, 2)
    df["option_type"] = rng.choice(["CALL", "PUT"], n)
    df["direction"] = rng.choice(["LONG", "SHORT"], n)
    df["exercise_style"] = "AMERICAN" if product == "amer_opt" else "EUROPEAN"
    df["dividend_yield"] = 0.015
    df["trade_date"] = (pd.Timestamp(as_of) - pd.to_timedelta(rng.integers(0, 120, n), unit="D")).date
    df["start_date"] = df["trade_date"]
    df["maturity_date"] = pd.to_datetime(mat).date
    return df


# ---------------------------------------------------------------------------
# Defects (quarantine candidates)
# ---------------------------------------------------------------------------
_COMMON_DEFECTS = ["null_notional", "null_maturity", "invalid_ccy", "null_counterparty", "null_book",
                   "maturity_before_start", "negative_notional"]
_PRODUCT_DEFECTS = {"irs": ["null_fixed_rate", "null_float_index"], "fra": ["null_fixed_rate"],
                    "xccy": ["null_fixed_rate", "null_ccy_2"], "fxfwd": ["null_forward_rate"],
                    "euro_opt": ["null_strike", "null_underlying"], "amer_opt": ["null_strike", "null_underlying"]}


def inject_defects(df: pd.DataFrame, product: str, n_bad: int, rng) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.copy()
    kinds = _COMMON_DEFECTS + _PRODUCT_DEFECTS[product]
    idx = rng.choice(len(df), size=min(n_bad, len(df)), replace=False)
    log = []
    for i in idx:
        k = kinds[int(rng.integers(0, len(kinds)))]
        col = {"null_notional": "notional", "null_maturity": "maturity_date", "null_counterparty": "counterparty_id",
               "null_book": "book_id", "null_fixed_rate": "fixed_rate", "null_float_index": "float_index",
               "null_ccy_2": "ccy_2", "null_forward_rate": "forward_rate", "null_strike": "strike",
               "null_underlying": "underlying"}.get(k)
        if col:
            df.at[df.index[i], col] = None
        elif k == "invalid_ccy":
            df.at[df.index[i], "ccy"] = "XXX"
        elif k == "negative_notional":
            df.at[df.index[i], "notional"] = -abs(df.at[df.index[i], "notional"])
        elif k == "maturity_before_start":
            sd = df.at[df.index[i], "start_date"]
            df.at[df.index[i], "maturity_date"] = (pd.Timestamp(sd) - pd.Timedelta(days=30)).date()
        log.append(dict(trade_id=df.at[df.index[i], "trade_id"], product=product, defect=k))
    return df, pd.DataFrame(log)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def generate_all(as_of, curves: dict, fx_spot: dict, eq_spot: dict, ref: dict, n_per_product: int = 52000,
                 n_bad_per_product: int = 100, seed: int = 11, start_ids: dict | None = None):
    """Return (clean trades by product, trades with defects by product, defect log)."""
    rng = np.random.default_rng(seed)
    start_ids = start_ids or {p: 1 for p in PRODUCTS}
    clean = {
        "irs": gen_irs(n_per_product, as_of, curves, rng, ref, start_ids["irs"]),
        "xccy": gen_xccy(n_per_product, as_of, curves, fx_spot, rng, ref, start_ids["xccy"]),
        "fra": gen_fra(n_per_product, as_of, curves, rng, ref, start_ids["fra"]),
        "fxfwd": gen_fxfwd(n_per_product, as_of, curves, fx_spot, rng, ref, start_ids["fxfwd"]),
        "euro_opt": gen_option("euro_opt", n_per_product, as_of, eq_spot, rng, ref, start_ids["euro_opt"]),
        "amer_opt": gen_option("amer_opt", n_per_product, as_of, eq_spot, rng, ref, start_ids["amer_opt"]),
    }
    dirty, logs = {}, []
    for p, df in clean.items():
        dirty[p], lg = inject_defects(df, p, n_bad_per_product, rng)
        logs.append(lg)
    return clean, dirty, pd.concat(logs, ignore_index=True)


def amend(df: pd.DataFrame, as_of, rng, frac: float = 0.01, big_frac: float = 0.2) -> pd.DataFrame:
    """Day-on-day amendments: notional changes (some > 20%) and maturity extensions. Returns changed rows."""
    n = max(1, int(len(df) * frac))
    sel = df.sample(n=n, random_state=int(rng.integers(0, 2**31 - 1))).copy()
    big = rng.random(n) < big_frac
    factor = np.where(big, rng.choice([0.3, 2.5, 3.0], n), rng.uniform(0.9, 1.1, n))
    sel["notional"] = np.round(sel["notional"].astype(float) * factor, 2)
    if "pay_freq_months" in sel and "maturity_date" in sel:
        ext = rng.random(n) < 0.2
        mats = pd.to_datetime(sel["maturity_date"]).values.astype("datetime64[D]")
        sel.loc[ext, "maturity_date"] = pd.to_datetime(add_months(mats[ext], 12)).date
    sel["version"] = sel["version"].astype(int) + 1
    sel["as_of_date"] = pd.Timestamp(as_of).date()
    return sel


def new_trades(as_of, curves, fx_spot, eq_spot, ref, next_ids: dict, n_new: int, seed: int):
    clean, dirty, log = generate_all(as_of, curves, fx_spot, eq_spot, ref, n_per_product=n_new,
                                     n_bad_per_product=max(1, n_new // 50), seed=seed, start_ids=next_ids)
    return clean, dirty, log
