"""Unit tests for the riskcore library (synthetic market data, no tables required).

Run:  pytest -q tests/   (QuantLib, numpy, pandas, scipy required)
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath("__file__"))  # works in both pytest and Databricks
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, os.path.join(_HERE, "src"))

from riskcore import corr, curves, exposure, hullwhite, pricers_ql, pricers_vec, sim, timegrid, tradegen, var  # noqa: E402

AS_OF = pd.Timestamp("2026-10-20")
USD_PAR = {"1M": 0.043, "3M": 0.042, "6M": 0.041, "1Y": 0.039, "2Y": 0.036, "3Y": 0.035, "5Y": 0.036,
           "7Y": 0.038, "10Y": 0.040, "20Y": 0.044, "30Y": 0.045}
FX = {"EUR": 1.08, "GBP": 1.30, "JPY": 1 / 150, "CHF": 1.12, "CAD": 0.73}
EQ = {"^GSPC": 5800.0, "^FTSE": 8300.0, "^STOXX50E": 4900.0, "^N225": 38000.0, "^SSMI": 12000.0,
      "^GSPTSE": 24000.0, "AAPL": 230.0, "JPM": 210.0, "SAP.DE": 200.0, "7203.T": 2700.0}


@pytest.fixture(scope="module")
def market():
    zeros = {"USD": curves.bootstrap_usd_treasury(AS_OF, USD_PAR)}
    for c, (s, l) in {"EUR": (0.020, 0.027), "GBP": (0.040, 0.044), "JPY": (0.004, 0.015),
                      "CHF": (0.005, 0.008), "CAD": (0.028, 0.032)}.items():
        zeros[c] = curves.curve_from_zero_points(AS_OF, {0.25: s, 10.0: l})
    names = corr.core_factor_names()
    C = pd.DataFrame(np.eye(len(names)), index=names, columns=names)
    C.loc["IR_USD", "IR_EUR"] = C.loc["IR_EUR", "IR_USD"] = 0.6
    eq_vol = {k: {0.25: 0.22, 1.0: 0.21, 3.0: 0.20, 10.0: 0.20} for k in EQ}
    fx_vol = {c: {0.25: 0.08, 1.0: 0.085, 3.0: 0.09, 10.0: 0.09} for c in FX}
    ms = sim.MarketState(AS_OF, zeros, {c: (0.15, 0.009) for c in zeros}, FX, fx_vol, EQ, eq_vol,
                         {"AAPL": 0.7, "JPM": 0.6, "SAP.DE": 0.6, "7203.T": 0.5}, C)
    cube = sim.simulate(ms, n_paths=400)
    ref = tradegen.reference_data()
    cv = {c: curves.ZeroCurve.from_tenors(z) for c, z in zeros.items()}
    clean, dirty, log = tradegen.generate_all(AS_OF, cv, FX, EQ, ref, n_per_product=300, n_bad_per_product=10)
    eqv = {k: {float(a): b for a, b in v.items()} for k, v in cube.eq_vol.items()}
    mkt = pricers_ql.QLMarket(AS_OF, zeros, FX, EQ, eqv, cube.eq_ccy)
    return dict(zeros=zeros, cube=cube, clean=clean, dirty=dirty, log=log, mkt=mkt, ref=ref)


def test_time_grid():
    g = timegrid.simulation_dates(AS_OF)
    assert g[0] == AS_OF and len(g) == 46 and g[-1] == pd.Timestamp("2036-10-20")


def test_bootstrap_reprices_par(market):
    z = market["zeros"]["USD"]
    assert abs(z["3M"] - 0.0418) < 5e-4 and 0.03 < z["10Y"] < 0.045


@pytest.mark.parametrize("vol1,vol10,n,case", [(0.0, 0.05, 2000, 1), (0.06, 0.05, 2000, 2),
                                                (0.006, 0.055, 100, 3), (0.009, 0.03, 2000, 7)])
def test_hw_calibration_cases(vol1, vol10, n, case):
    assert hullwhite.calibrate(vol1, vol10, n)["calibration_case"] == case


def test_hw_martingale(market):
    cube = market["cube"]
    r = np.array([cube.short_rate("USD", s) for s in range(cube.n_steps)])
    dt = np.diff(cube.t)[:, None]
    D = np.exp(-np.concatenate([np.zeros((1, r.shape[1])), np.cumsum(0.5 * (r[1:] + r[:-1]) * dt, 0)]))
    for s in (20, 37):
        assert abs(D[s].mean() / cube.curves["USD"].df(cube.t[s]) - 1) < 2e-3


def test_trade_population_rules(market):
    allt = pd.concat(market["clean"].values(), ignore_index=True)
    assert (allt.netting_set_id == "NA").mean() >= 1 / 3
    assert (allt.collateral_id == "NA").mean() >= 1 / 3
    netted = allt[allt.netting_set_id != "NA"]
    assert (netted.groupby("netting_set_id").counterparty_id.nunique() == 1).all()
    assert len(market["log"]) == 60


@pytest.mark.parametrize("product", tradegen.PRODUCTS)
def test_vectorised_matches_quantlib(market, product):
    d = market["clean"][product].head(40).reset_index(drop=True)
    v = pricers_vec.price_trades(d, market["cube"], steps=[0])[:, 0, 0]
    q = pricers_ql.price_frame(d, market["mkt"])
    assert not np.isnan(q).any()
    assert np.max(np.abs(v - q) / d.notional.abs().to_numpy()) < 1e-4      # < 1bp of notional


def test_exposure_known_case():
    acc = exposure.Accumulator(2, 4)
    tr = pd.DataFrame({"trade_id": ["A", "B", "C"], "product": ["irs"] * 3, "counterparty_id": ["X"] * 3,
                       "netting_set_id": ["NS-X-1", "NS-X-1", "NA"], "book_id": ["B1", "B1", "B2"]})
    V = np.array([[[10, -5, 3, 0], [1, 1, 1, 1]], [[-4, -4, -4, -4], [0, 0, 0, 0]],
                  [[-1, 2, -3, 4], [5, 5, 5, 5]]], float)
    acc.add(tr, V)
    e = acc.exposure_rows()
    row = e[(e.netting_set_id == "ALL") & (e.time_step == 0)].iloc[0]
    assert row.ee == pytest.approx(3.0)
    assert row.pfe_95 == pytest.approx(np.percentile([6, 2, 0, 4], 95))


def test_distributed_equals_single_pass(market):
    allt = pd.concat(market["clean"].values(), ignore_index=True).sample(200, random_state=3).reset_index(drop=True)
    V = pricers_vec.price_trades(allt, market["cube"])
    acc = exposure.Accumulator(*V.shape[1:]); acc.add(allt, V)
    ref = acc.exposure_rows()
    bins = np.random.default_rng(0).integers(0, 3, len(allt))
    parts = []
    for b in range(3):
        m = bins == b
        a = exposure.Accumulator(*V.shape[1:]); a.add(allt[m].reset_index(drop=True), V[m]); parts.append(a.output())
    parts = pd.concat(parts, ignore_index=True)
    fin = pd.concat([exposure.finalize_counterparty(g)
                     for _, g in parts[parts.record_type.str.startswith("PART")].groupby("counterparty_id")])
    m = ref.merge(fin, on=["counterparty_id", "netting_set_id", "time_step"], suffixes=("_r", "_d"))
    assert len(m) == len(ref)
    assert np.allclose(m.ee_r, m.ee_d) and np.allclose(m.pfe_95_r, m.pfe_95_d)


def test_book_var_definition():
    out = var.book_var("B", np.array([[6, 6, 6, 6], [1, 2, 3, 10.0]]))
    assert out.var_95.iloc[0] == 0 and out.var_95.iloc[1] == pytest.approx(np.percentile([5, 4, 3, -4], 95))


def test_bin_packing_balances():
    bins = exposure.assign_bins(pd.DataFrame({"key": ["big", "a", "b"], "n": [6000, 700, 900]}), 1500, 4)
    assert bins[bins.key == "big"].n_chunks.iloc[0] == 4 and bins.bin.nunique() == 4


if __name__ == "__main__":
    _test_path = "/Workspace/Users/brian.mcconnell@databricks.com/derivatives-risk-platform/tests/test_riskcore.py"
    pytest.main([_test_path, "-v", "--tb=short", "-p", "no:cacheprovider", "--assert=plain"])
