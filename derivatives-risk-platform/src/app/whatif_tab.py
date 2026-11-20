"""Trade what-if tab: enter a hypothetical trade, price it, compute its sensitivities and its impact on the
book's VaR / expected shortfall, the firm VaR and the counterparty's EE / PFE.

The trade is priced with the batch pricers (riskcore) on the batch simulation cube, and added path by path to
the batch portfolio paths written by 13_calc_exposure (sim_cubes/portfolio_<as_of>/, read through the app's
volume resource), so "before" equals the published batch numbers and "after" is exact for the same scenarios.

What-if trades never touch the silver trade tables. They are stored in Lakebase:
  riskapp_whatif.trades   one row per distinct trade (spec as entered, JSONB), reusable from the picker
  riskapp_whatif.runs     one row per run (valuation date, headline before / after numbers, JSONB summary)
Every run is also audited in the lakehouse (market.whatif_run_audit, see audit.py): headline numbers, the trade,
when it was calculated and who ran it (run_by masked for non-admins).
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import psycopg
import streamlit as st
from databricks.sdk import WorkspaceClient
from psycopg.types.json import Jsonb

import audit
import db

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
from riskcore import whatif as wi          # noqa: E402
from riskcore.sim import SimCube           # noqa: E402

CUBE_DIR = os.environ.get("SIM_CUBE_DIR", "/Volumes/serverless_stable_7s0j2u_catalog/market/sim_cubes")
SCHEMA = os.environ.get("WHATIF_SCHEMA", "riskapp_whatif")     # override only for tests
_w = WorkspaceClient()

# product fields as (spec field, kind); widget keys are wi_<product>_<field>
FIELDS = {
    "irs": [("ccy", "str"), ("notional", "num"), ("pay_freq_months", "int"), ("start_date", "date"),
            ("maturity_date", "date"), ("fixed_rate", "pct")],
    "xccy": [("ccy_2", "str"), ("notional", "num"), ("notional_2", "num"), ("pay_freq_months", "int"),
             ("start_date", "date"), ("maturity_date", "date"), ("fixed_rate", "pct"), ("fixed_rate_2", "pct")],
    "fra": [("ccy", "str"), ("notional", "num"), ("start_date", "date"), ("end_date", "date"), ("fixed_rate", "pct")],
    "fxfwd": [("ccy", "str"), ("notional", "num"), ("maturity_date", "date"), ("forward_rate", "num")],
    "euro_opt": [("underlying", "str"), ("option_type", "str"), ("strike", "num"), ("quantity", "num"),
                 ("maturity_date", "date"), ("dividend_yield", "pct")],
}
FIELDS["amer_opt"] = FIELDS["euro_opt"]
DIR_LABEL = {"PAY": "Pay fixed", "RECEIVE": "Receive fixed", "RECEIVE_CCY2": "Receive foreign leg, pay USD",
             "PAY_CCY2": "Pay foreign leg, receive USD", "BUY": "Buy", "SELL": "Sell", "LONG": "Long (buy)",
             "SHORT": "Short (sell)"}


def k(p: str, f: str) -> str:
    return f"wi_{p}_{f}"


def money(v: float | None, unit: str = "m") -> str:
    if v is None or not np.isfinite(v):
        return "-"
    return f"${v / 1e6:,.2f}m" if unit == "m" else f"${v:,.0f}"


# ---------------------------------------------------------------------------
# Lakebase: what-if trades and runs (owned by the app service principal)
# ---------------------------------------------------------------------------
DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.trades (
    trade_id        text PRIMARY KEY,
    label           text,
    product         text NOT NULL CHECK (product IN ('irs','xccy','fra','fxfwd','euro_opt','amer_opt')),
    book_id         text NOT NULL,
    counterparty_id text NOT NULL,
    netting_set_id  text NOT NULL DEFAULT 'NA',
    spec            jsonb NOT NULL,
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    is_archived     boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.runs (
    run_id               bigserial PRIMARY KEY,
    trade_id             text NOT NULL REFERENCES {SCHEMA}.trades(trade_id),
    run_at               timestamptz NOT NULL DEFAULT now(),
    run_by               text,
    valuation_date       date NOT NULL,
    mtm_usd              double precision,
    book_var_1d_before   double precision,
    book_var_1d_after    double precision,
    book_es_1d_before    double precision,
    book_es_1d_after     double precision,
    peak_pfe_before      double precision,
    peak_pfe_after       double precision,
    results              jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_trade_idx ON {SCHEMA}.runs (trade_id, run_at DESC);
"""


@st.cache_resource(show_spinner=False)
def ensure_schema() -> bool:
    db.execute(DDL)
    return True


def saved_trades() -> pd.DataFrame:
    return db.query(f"""SELECT t.trade_id, t.label, t.product, t.book_id, t.counterparty_id, t.netting_set_id,
                              t.created_by, t.created_at, max(r.run_at) AS last_run, count(r.run_id) AS n_runs
                       FROM {SCHEMA}.trades t LEFT JOIN {SCHEMA}.runs r USING (trade_id)
                       WHERE NOT t.is_archived GROUP BY t.trade_id ORDER BY coalesce(max(r.run_at), t.created_at) DESC
                       LIMIT 500""")


def saved_spec(trade_id: str) -> dict | None:
    r = db.query(f"SELECT spec, label FROM {SCHEMA}.trades WHERE trade_id = %s", (trade_id,))
    if r.empty:
        return None
    spec = r.spec.iloc[0]
    spec = json.loads(spec) if isinstance(spec, str) else dict(spec)
    spec["label"] = r.label.iloc[0]
    return spec


def trade_runs(trade_id: str) -> pd.DataFrame:
    return db.query(f"""SELECT run_at, run_by, valuation_date, mtm_usd, book_var_1d_before, book_var_1d_after,
                              book_es_1d_before, book_es_1d_after, peak_pfe_before, peak_pfe_after
                       FROM {SCHEMA}.runs WHERE trade_id = %s ORDER BY run_at DESC LIMIT 50""", (trade_id,))


def _norm(spec: dict) -> str:
    keep = {kk: v for kk, v in spec.items() if kk not in ("label", "trade_id")}
    return json.dumps(keep, sort_keys=True, default=str)


def store_trade(spec: dict, label: str, user: str | None, reuse_id: str | None) -> str:
    """Trades are immutable: an unchanged reloaded trade keeps its id, anything else gets a new one."""
    if reuse_id:
        old = saved_spec(reuse_id)
        if old is not None and _norm(old) == _norm(spec):
            if (label or None) != (old.get("label") or None):
                db.execute(f"UPDATE {SCHEMA}.trades SET label = %s WHERE trade_id = %s", (label or None, reuse_id))
            return reuse_id
    tid = wi.new_trade_id()
    db.execute(f"""INSERT INTO {SCHEMA}.trades (trade_id, label, product, book_id, counterparty_id, netting_set_id,
                                                spec, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
               (tid, label or None, spec["product"], spec["book_id"], spec["counterparty_id"],
                spec.get("netting_set_id") or wi.NA, Jsonb(json.loads(json.dumps(spec, default=str))), user))
    return tid


def store_run(trade_id: str, user: str | None, as_of: dt.date, mtm: float, res: dict) -> int:
    b, cp = res["book"], res["counterparty"]
    r = db.query(f"""INSERT INTO {SCHEMA}.runs (trade_id, run_by, valuation_date, mtm_usd, book_var_1d_before,
                       book_var_1d_after, book_es_1d_before, book_es_1d_after, peak_pfe_before, peak_pfe_after, results)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id""",
               (trade_id, user, as_of, mtm, b["before"].get("var_1-day"), b["after"].get("var_1-day"),
                b["before"].get("es_1-day"), b["after"].get("es_1-day"), cp["before"]["peak_pfe"],
                cp["after"]["peak_pfe"], Jsonb(json.loads(json.dumps(wi.summary_json(mtm, res), default=float)))))
    return int(r.run_id.iloc[0])


# ---------------------------------------------------------------------------
# Batch simulation cube and portfolio paths (UC volume)
# ---------------------------------------------------------------------------
def _read(path: str) -> bytes | None:
    try:
        return _w.files.download(path).contents.read()
    except Exception as e:                       # missing file -> None; anything else surfaces
        if "NOT_FOUND" in str(e).upper() or "404" in str(e) or "does not exist" in str(e).lower():
            return None
        raise


@st.cache_data(ttl=600, show_spinner=False)
def latest_portfolio() -> tuple[str, dict] | None:
    names = sorted((e.name.rstrip("/") for e in _w.files.list_directory_contents(CUBE_DIR)
                    if e.is_directory and e.name.startswith(wi.PORTFOLIO_PREFIX)), reverse=True)
    for n in names:
        m = _read(f"{CUBE_DIR}/{n}/manifest.json")
        if m:
            return f"{CUBE_DIR}/{n}", json.loads(m)
    return None


@st.cache_resource(max_entries=2, show_spinner="Loading the batch simulation...")
def load_market(pdir: str, cube_file: str):
    manifest = json.loads(_read(f"{pdir}/manifest.json"))
    cube = SimCube.load(io.BytesIO(_read(f"{CUBE_DIR}/{cube_file}")))
    port = wi.Portfolio(manifest, _read(f"{pdir}/books.npz"), json.loads(_read(f"{pdir}/refdata.json")))
    return cube, port


@st.cache_data(max_entries=64, show_spinner=False)
def cpty_blob(pdir: str, cpty: str) -> bytes | None:
    return _read(f"{pdir}/cpty/{cpty}.npz")


# ---------------------------------------------------------------------------
# Form state <-> trade spec
# ---------------------------------------------------------------------------
def _add_years(d: dt.date, years: int) -> dt.date:
    return (pd.Timestamp(d) + pd.DateOffset(years=years)).date()


def state_to_spec(cube, ref) -> dict:
    s = st.session_state
    p = s["wi_product"]
    cp = s["wi_counterparty_id"]
    ns = s.get(f"wi_ns_{cp}", wi.NA)
    spec = dict(product=p, book_id=s["wi_book_id"], counterparty_id=cp, netting_set_id=ns,
                collateral_id=collateral_for(ref, ns), direction=s[k(p, "direction")])
    for f, kind in FIELDS[p]:
        v = s.get(k(p, f))
        if kind == "pct":
            v = None if v is None else float(v) / 100.0
        elif kind == "date":
            v = None if v is None else v.isoformat()
        elif kind == "num":
            v = None if v is None else float(v)
        elif kind == "int":
            v = None if v is None else int(v)
        spec[f] = v
    return spec


def spec_to_state(spec: dict):
    s = st.session_state
    p = spec["product"]
    s["wi_product"] = p
    s["wi_label"] = spec.get("label") or ""
    s["wi_book_id"] = spec["book_id"]
    s["wi_counterparty_id"] = spec["counterparty_id"]
    s[f"wi_ns_{spec['counterparty_id']}"] = spec.get("netting_set_id") or wi.NA
    s[k(p, "direction")] = spec["direction"]
    for f, kind in FIELDS[p]:
        v = spec.get(f)
        if v is None:
            s.pop(k(p, f), None)
        elif kind == "pct":
            s[k(p, f)] = float(v) * 100.0
        elif kind == "date":
            s[k(p, f)] = dt.date.fromisoformat(str(v)[:10])
        elif kind == "int":
            s[k(p, f)] = int(v)
        elif kind == "num":
            s[k(p, f)] = float(v)
        else:
            s[k(p, f)] = v


def collateral_for(ref: dict, ns: str) -> str:
    for c in ref["collateral"]:
        if c["netting_set_id"] == ns:
            return c["collateral_id"]
    return wi.NA


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(user: str | None):
    try:
        ensure_schema()
        found = latest_portfolio()
    except Exception as e:
        st.error(f"What-if storage or simulation files are not reachable: {e}")
        return
    if not found:
        st.warning("No batch portfolio paths yet. They are written by the daily risk run (13_calc_exposure).")
        return
    pdir, manifest = found
    cube, port = load_market(pdir, manifest["cube_file"])
    ref = port.ref
    mk = wi.market_snapshot(cube)
    A = mk["as_of"]
    st.caption(f"Valuation date {A}. The trade is priced on the batch simulation ({manifest['n_paths']:,} paths, "
               f"{manifest['n_steps']} time steps) and added to the batch book and counterparty paths "
               f"({manifest['n_trades']:,} live trades), so 'before' matches the published risk numbers. "
               "What-if trades are kept in Lakebase only, never in the trade tables. Exposures are uncollateralised, "
               "as in the batch.")

    books = {b["book_id"]: b for b in ref["books"]}
    cptys = {c["counterparty_id"]: c for c in ref["counterparties"]}
    eqs = {e["ticker"]: e for e in ref["equities"]}
    ccys = mk["ccys"]
    fx_ccys = [c for c in ccys if c != "USD"]
    s = st.session_state

    # ---- saved what-if trades -------------------------------------------
    saved = saved_trades()
    if (pending := s.pop("wi_pick_pending", None)) is not None:     # select the trade just run
        s["wi_pick"] = pending
    pick_col, b1, b2, b3 = st.columns([4, 1, 1, 1])
    ids = saved.trade_id.tolist()
    lab = {r.trade_id: f"{r.trade_id} | {wi.PRODUCT_LABELS[r.product]} | {r.book_id} | {r.counterparty_id}"
                       + (f" | {r.label}" if r.label else "") + f" | {r.created_by or ''}" for r in saved.itertuples()}
    pick = pick_col.selectbox("Saved what-if trades", [""] + ids, key="wi_pick",
                              format_func=lambda x: lab.get(x, "New trade") if x else "New trade")

    def cb_load(rerun: bool):
        spec = saved_spec(s["wi_pick"]) if s.get("wi_pick") else None
        if spec:
            spec_to_state(spec)
            s["wi_loaded"] = s["wi_pick"]
            s["wi_autorun"] = rerun

    def cb_new():
        for kk in [kk for kk in s.keys() if str(kk).startswith("wi_") and kk not in ("wi_pick",)]:
            del s[kk]
        s["wi_pick"] = ""

    b1.button("Load", on_click=cb_load, args=(False,), disabled=not pick, use_container_width=True)
    b2.button("Load and rerun", on_click=cb_load, args=(True,), disabled=not pick, use_container_width=True)
    b3.button("New trade", on_click=cb_new, use_container_width=True)

    # ---- trade entry ----------------------------------------------------
    s.setdefault("wi_product", "irs")
    s.setdefault("wi_book_id", sorted(books)[0])
    s.setdefault("wi_counterparty_id", sorted(cptys)[0])
    s.setdefault("wi_label", "")
    st.subheader("Trade")
    c1, c2, c3, c4 = st.columns(4)
    p = c1.selectbox("Product", wi.PRODUCTS, key="wi_product", format_func=wi.PRODUCT_LABELS.get)
    c2.selectbox("Book", sorted(books), key="wi_book_id",
                 format_func=lambda b: f"{b} | {books[b]['desk']} | {books[b]['business_unit']}")
    cp = c3.selectbox("Counterparty", sorted(cptys), key="wi_counterparty_id",
                      format_func=lambda c: f"{c} | {cptys[c]['name']} | {cptys[c]['rating']}")
    ns_rows = [n for n in ref["netting_sets"] if n["counterparty_id"] == cp]
    ns_opts = [n["netting_set_id"] for n in ns_rows] + [wi.NA]
    ns_lab = {n["netting_set_id"]: f"{n['netting_set_id']} | {n['agreement_type']}, {n['governing_law']} law"
              for n in ns_rows}
    ns_lab[wi.NA] = "No netting agreement"
    ns = c4.selectbox("Netting set", ns_opts, key=f"wi_ns_{cp}", format_func=ns_lab.get)
    csa = next((c for c in ref["collateral"] if c["netting_set_id"] == ns), None)
    cpy = cptys[cp]
    st.caption(f"{cpy['name']}: rating {cpy['rating']}, {cpy['sector']}, {cpy['country']}. Collateral: "
               + (f"{csa['collateral_id']} ({csa['agreement_status']}, {csa['csa_ccy']}, threshold "
                  f"{money(csa['threshold'])}, MTA {money(csa['mta'])}, MPoR {csa['mpor_days']}d) - recorded with the "
                  "trade; exposure is shown uncollateralised." if csa else "none."))

    d1, d2 = st.columns([1, 3])
    s.setdefault(k(p, "direction"), wi.DIRECTIONS[p][0])
    d1.selectbox("Direction", wi.DIRECTIONS[p], key=k(p, "direction"), format_func=DIR_LABEL.get)
    d2.text_input("Label (optional)", key="wi_label", placeholder="e.g. hedge for BK001 5Y EUR risk")

    def market_cb(field: str | None = None):
        try:
            spec = state_to_spec(cube, ref)
            if p == "xccy":
                usd = dict(spec, product="irs", ccy="USD", direction="PAY")
                s[k(p, "fixed_rate")] = wi.solve_at_market(usd, cube) * 100.0
                spec = state_to_spec(cube, ref)
                s[k(p, "fixed_rate_2")] = wi.solve_at_market(spec, cube) * 100.0
            elif p == "fxfwd":
                s[k(p, "forward_rate")] = wi.solve_at_market(spec, cube)
            elif p in ("euro_opt", "amer_opt"):
                s[k(p, "strike")] = round(eqs[spec["underlying"]]["spot"], 4)
            else:
                s[k(p, "fixed_rate")] = wi.solve_at_market(spec, cube) * 100.0
        except Exception as e:
            s["wi_err"] = f"Could not set the market level: {e}"

    f1, f2, f3, f4 = st.columns(4)
    if p in ("irs", "fra"):
        s.setdefault(k(p, "ccy"), "USD")
        f1.selectbox("Currency", ccys, key=k(p, "ccy"))
        s.setdefault(k(p, "notional"), 50e6 if p == "irs" else 100e6)
        f2.number_input("Notional (currency units)", min_value=0.0, step=1e6, format="%.0f", key=k(p, "notional"))
        if p == "irs":
            s.setdefault(k(p, "pay_freq_months"), 3)
            f3.selectbox("Payment frequency (months)", [1, 3, 6, 12], key=k(p, "pay_freq_months"))
            s.setdefault(k(p, "start_date"), A)
            f4.date_input("Start date", key=k(p, "start_date"))
            g1, g2, g3, _ = st.columns(4)
            s.setdefault(k(p, "maturity_date"), _add_years(A, 5))
            g1.date_input("Maturity date", key=k(p, "maturity_date"))
        else:
            s.setdefault(k(p, "start_date"), A + dt.timedelta(days=91))
            f3.date_input("Settlement (start) date", key=k(p, "start_date"))
            s.setdefault(k(p, "end_date"), A + dt.timedelta(days=182))
            f4.date_input("End date", key=k(p, "end_date"))
            g1, g2, g3, _ = st.columns(4)
        if k(p, "fixed_rate") not in s:
            market_cb()
        g2.number_input("Fixed rate (%)", step=0.01, format="%.4f", key=k(p, "fixed_rate"))
        g3.button("Set fixed rate at market", on_click=market_cb, use_container_width=True)
    elif p == "xccy":
        s.setdefault(k(p, "ccy_2"), "EUR")
        f1.selectbox("Foreign currency (USD vs)", fx_ccys, key=k(p, "ccy_2"))
        s.setdefault(k(p, "notional"), 50e6)
        f2.number_input("USD notional", min_value=0.0, step=1e6, format="%.0f", key=k(p, "notional"))
        spot2 = mk["fx"][s[k(p, "ccy_2")]]
        s.setdefault(k(p, "notional_2"), 0.0)
        f3.number_input("Foreign notional (0 = USD notional / spot)", min_value=0.0, step=1e6,
                        format="%.0f", key=k(p, "notional_2"),
                        help=f"Spot {spot2:.6g} USD per {s[k(p, 'ccy_2')]}; 0 gives "
                             f"{s[k(p, 'notional')] / spot2:,.0f} {s[k(p, 'ccy_2')]}")
        s.setdefault(k(p, "pay_freq_months"), 6)
        f4.selectbox("Payment frequency (months)", [3, 6, 12], key=k(p, "pay_freq_months"))
        g1, g2, g3, g4 = st.columns(4)
        s.setdefault(k(p, "start_date"), A)
        g1.date_input("Start date", key=k(p, "start_date"))
        s.setdefault(k(p, "maturity_date"), _add_years(A, 5))
        g2.date_input("Maturity date", key=k(p, "maturity_date"))
        if k(p, "fixed_rate_2") not in s:
            market_cb()
        g3.number_input("USD fixed rate (%)", step=0.01, format="%.4f", key=k(p, "fixed_rate"))
        g4.number_input("Foreign fixed rate (%)", step=0.01, format="%.4f", key=k(p, "fixed_rate_2"))
        st.button("Set both rates at market", on_click=market_cb)
    elif p == "fxfwd":
        s.setdefault(k(p, "ccy"), "EUR")
        f1.selectbox("Currency (vs USD)", fx_ccys, key=k(p, "ccy"))
        s.setdefault(k(p, "notional"), 25e6)
        f2.number_input("Notional (foreign currency units)", min_value=0.0, step=1e6, format="%.0f",
                        key=k(p, "notional"))
        s.setdefault(k(p, "maturity_date"), A + dt.timedelta(days=182))
        f3.date_input("Value date", key=k(p, "maturity_date"))
        if k(p, "forward_rate") not in s:
            market_cb()
        f4.number_input("Forward rate (USD per unit of currency)", min_value=0.0, step=0.0001, format="%.6f",
                        key=k(p, "forward_rate"), help=f"Spot {mk['fx'][s[k(p, 'ccy')]]:.6g} USD per {s[k(p, 'ccy')]}")
        st.button("Set forward rate at market", on_click=market_cb)
    else:
        tick = sorted(eqs)
        s.setdefault(k(p, "underlying"), "^GSPC" if "^GSPC" in eqs else tick[0])
        f1.selectbox("Underlying", tick, key=k(p, "underlying"),
                     format_func=lambda t: f"{t} | {eqs[t]['ccy']} | spot {eqs[t]['spot']:,.2f}")
        s.setdefault(k(p, "option_type"), "CALL")
        f2.selectbox("Call / put", ["CALL", "PUT"], key=k(p, "option_type"))
        und = s[k(p, "underlying")]
        if k(p, "strike") not in s:
            s[k(p, "strike")] = round(eqs[und]["spot"], 4)
        f3.number_input("Strike", min_value=0.0, step=1.0, format="%.4f", key=k(p, "strike"))
        s.setdefault(k(p, "quantity"), 1000.0)
        f4.number_input("Quantity (units)", min_value=0.0, step=100.0, format="%.0f", key=k(p, "quantity"))
        g1, g2, g3, _ = st.columns(4)
        s.setdefault(k(p, "maturity_date"), _add_years(A, 1))
        g1.date_input("Expiry date", key=k(p, "maturity_date"))
        s.setdefault(k(p, "dividend_yield"), eqs[und]["div_yield"] * 100.0)
        g2.number_input("Dividend yield (%)", min_value=0.0, step=0.1, format="%.3f", key=k(p, "dividend_yield"))
        g3.button("Strike at the money", on_click=market_cb, use_container_width=True)

    if err := s.pop("wi_err", None):
        st.error(err)

    run = st.button("Run what-if", type="primary") or s.pop("wi_autorun", False)
    if run:
        try:
            spec = state_to_spec(cube, ref)
            tr = wi.build_trade(spec, cube)
        except (ValueError, KeyError) as e:
            st.error(f"Trade is not valid: {e}")
            tr = None
        if tr is not None:
            with st.spinner("Pricing, computing sensitivities and re-aggregating risk..."):
                t_start = dt.datetime.now(dt.timezone.utc)
                try:
                    tid = store_trade(spec, s.get("wi_label", ""), user, s.get("wi_loaded"))
                except psycopg.Error as e:
                    st.error(f"Could not save the what-if trade: {str(e).splitlines()[0]}")
                    tid = None
                tr["trade_id"] = tid or "UNSAVED"
                mtm, sens = wi.sensitivities(tr, cube)
                V = wi.price_paths(tr, cube)
                parts = wi.cpty_paths(cpty_blob(pdir, spec["counterparty_id"]))
                res = wi.impact(V, tr, port, parts, cube.t, cube.dates)
                calculated_at = dt.datetime.now(dt.timezone.utc)
                run_id = None
                if tid:
                    try:
                        run_id = store_run(tid, user, A, mtm, res)
                    except psycopg.Error as e:
                        st.warning(f"Run not recorded: {str(e).splitlines()[0]}")
                    s["wi_loaded"] = tid
            with st.spinner("Writing the audit record..."):
                aud = audit.write(audit.row(
                    user=user, trade=tr, res=res, mtm=mtm, trade_id=tid or "UNSAVED", label=s.get("wi_label", ""),
                    lakebase_run_id=run_id, as_of=A, cube_file=manifest["cube_file"], n_paths=manifest["n_paths"],
                    calculated_at=calculated_at, compute_seconds=(calculated_at - t_start).total_seconds()))
            s["wi_result"] = dict(trade_id=tid, trade=tr, mtm=mtm, sens=sens, res=res, as_of=A, audit=aud,
                                  calculated_at=calculated_at)
            if tid:
                s["wi_pick_pending"] = tid
                st.rerun()                      # refresh the saved-trade picker; results stay in session state

    if "wi_result" in s:
        show_result(s["wi_result"])


def _row(name, before, after):
    ch = after - before
    return {"Measure": name, "Before": money(before), "After": money(after), "Change": money(ch),
            "Change %": f"{100 * ch / abs(before):+.2f}%" if before else "-"}


def show_result(r: dict):
    res, tr = r["res"], r["trade"].iloc[0]
    b, f, cp, t = res["book"], res["firm"], res["counterparty"], res["trade"]
    st.divider()
    st.subheader(f"Results for {r['trade_id'] or 'unsaved trade'} ({wi.PRODUCT_LABELS[tr['product']]}, "
                 f"valued {r['as_of']})")
    status, detail = r.get("audit", ("failed", "not attempted"))
    when = r["calculated_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if r.get("calculated_at") else ""
    if status == "failed":
        st.warning(f"Calculated {when}. The audit record could not be written to {audit.TABLE}: {detail}")
    else:
        st.caption(f"Calculated {when}. Audit record {detail} "
                   + ("written to " if status == "written" else "being written to ") + audit.TABLE + ".")
    m = st.columns(5)
    m[0].metric("Trade MTM", money(r["mtm"]))
    m[1].metric(f"Book {b['book_id']} 1-day VaR95", money(b["after"]["var_1-day"]),
                money(b["after"]["var_1-day"] - b["before"]["var_1-day"]), delta_color="inverse")
    m[2].metric(f"Book {b['book_id']} 1-day ES95", money(b["after"]["es_1-day"]),
                money(b["after"]["es_1-day"] - b["before"]["es_1-day"]), delta_color="inverse")
    m[3].metric(f"{tr['counterparty_id']} peak PFE95", money(cp["after"]["peak_pfe"]),
                money(cp["after"]["peak_pfe"] - cp["before"]["peak_pfe"]), delta_color="inverse")
    m[4].metric("Firm 1-day VaR95", money(f["after"]["var_1-day"]),
                money(f["after"]["var_1-day"] - f["before"]["var_1-day"]), delta_color="inverse")

    left, right = st.columns(2)
    with left:
        st.markdown(f"**Market risk: book {b['book_id']} ({b['n_trades_before']:,} trades) and firm**")
        rows = []
        for lab in wi.VAR_STEPS:
            rows.append(_row(f"Book {lab} VaR95", b["before"][f"var_{lab}"], b["after"][f"var_{lab}"]))
            rows.append(_row(f"Book {lab} ES95", b["before"][f"es_{lab}"], b["after"][f"es_{lab}"]))
        for lab in wi.VAR_STEPS:
            rows.append(_row(f"Firm {lab} VaR95", f["before"][f"var_{lab}"], f["after"][f"var_{lab}"]))
            rows.append(_row(f"Firm {lab} ES95", f["before"][f"es_{lab}"], f["after"][f"es_{lab}"]))
        rows.append(_row("Book value today", b["before"]["t0_pv"], b["after"]["t0_pv"]))
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(f"Trade on its own: 1-day VaR95 {money(t['var_1-day'])}, ES95 {money(t['es_1-day'])}; "
                   f"10-day VaR95 {money(t['var_10-day'])}. Diversification = stand-alone VaR minus the change in "
                   f"book VaR: {money(t['var_1-day'] - (b['after']['var_1-day'] - b['before']['var_1-day']))}.")
    with right:
        st.markdown(f"**Credit risk: {tr['counterparty_id']} ({cp['n_trades_before']:,} trades), "
                    f"netting set {cp['netting_key']}**")
        rows = [_row("Peak PFE95", cp["before"]["peak_pfe"], cp["after"]["peak_pfe"]),
                _row("Peak EE", cp["before"]["peak_ee"], cp["after"]["peak_ee"]),
                _row("EPE (1 year)", cp["before"]["epe_1y"], cp["after"]["epe_1y"])]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(f"Trade on its own: peak PFE95 {money(cp['trade']['peak_pfe'])}, peak EE "
                   f"{money(cp['trade']['peak_ee'])}, EPE (1 year) {money(cp['trade']['epe_1y'])}.")

    prof = res["profile"]
    c1, c2 = st.columns(2)
    with c1:
        fig = go.Figure()
        for col, name, dash in [("pfe_before", "PFE95 before", "dot"), ("pfe_after", "PFE95 after", None),
                                ("ee_before", "EE before", "dot"), ("ee_after", "EE after", None)]:
            fig.add_trace(go.Scatter(x=prof.sim_date, y=prof[col] / 1e6, name=name, mode="lines",
                                     line=dict(dash=dash)))
        fig.update_layout(title=f"{tr['counterparty_id']} exposure profile (USD m)", height=380,
                          margin=dict(t=50, b=30), legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        pnl = res["pnl_1d"]
        fig = go.Figure()
        fig.add_trace(go.Histogram(x=pnl["book_before"] / 1e6, name="before", opacity=0.55, nbinsx=60))
        fig.add_trace(go.Histogram(x=pnl["book_after"] / 1e6, name="after", opacity=0.55, nbinsx=60))
        for key, col in [("before", "#636efa"), ("after", "#ef553b")]:
            fig.add_vline(x=-b[key]["var_1-day"] / 1e6, line_dash="dash", line_color=col)
        fig.update_layout(barmode="overlay", title=f"Book {b['book_id']} 1-day P&L across paths (USD m, dashed = -VaR95)",
                          height=380, margin=dict(t=50, b=30), legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True)

    sens = r["sens"]
    st.markdown("**Sensitivities (batch bump definitions: IR +1bp, FX and equity spot +1%, vol +1 point)**")
    if sens.empty:
        st.write("No non-zero sensitivities.")
    else:
        view = sens.assign(value_usd=sens.value_usd.round(2)).rename(columns={
            "sensitivity_type": "Type", "risk_class": "Class", "risk_factor": "Factor", "tenor_label": "Tenor",
            "value_usd": "USD"})
        st.dataframe(view, hide_index=True, use_container_width=True, height=min(400, 38 + 35 * len(view)))

    with st.expander("Priced trade (all fields) and run history"):
        st.dataframe(r["trade"].T.rename(columns={0: "value"}).astype(str), use_container_width=True)
        if r["trade_id"]:
            st.dataframe(trade_runs(r["trade_id"]), hide_index=True, use_container_width=True)
