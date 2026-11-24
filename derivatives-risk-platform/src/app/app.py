"""risk-curves-app: historical yield curves, simulated rate distributions and a user-maintained tenor list.

Data lives in Lakebase project `riskapp` (database databricks_postgres):
  riskapp.*      read-only synced copies of lakehouse tables (reference data, curve history, simulation state)
  riskapp_cfg.*  writable tenor list + audit trail maintained from this app
  riskapp_whatif.* what-if trades and their runs (Trade what-if tab, see whatif_tab.py)
Simulated rates for any tenor are rebuilt on the fly from the stored Hull-White 1F state (see riskmath.py),
so tenor edits take effect immediately without rerunning the simulation.
"""
from __future__ import annotations

import os
import datetime as dt

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import psycopg
import streamlit as st

import db
from riskmath import HW1F, ZeroCurve, simulated_rate, tenor_years

st.set_page_config(page_title="Risk Curves", layout="wide")

EDITORS = {e.strip().lower() for e in os.environ.get("APP_EDITORS", "").split(",") if e.strip()}
PCTS = [1, 5, 25, 50, 75, 95, 99]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def current_user() -> str | None:
    try:
        h = st.context.headers
        return h.get("X-Forwarded-Email") or h.get("X-Forwarded-Preferred-Username")
    except Exception:
        return None


USER = current_user()
CAN_EDIT = bool(USER) and USER.lower() in EDITORS


# ---------------------------------------------------------------------------
# Data access (reference data cached; the tenor list is always read fresh)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def currencies() -> pd.DataFrame:
    return db.query("SELECT ccy, ccy_name, first_curve_date, last_curve_date, n_curve_dates, is_simulated, "
                    "hw_alpha, hw_sigma, sim_as_of_date FROM riskapp.ref_currency ORDER BY ccy")


@st.cache_data(ttl=600, show_spinner=False)
def curve_dates(ccy: str) -> list[dt.date]:
    return db.query("SELECT as_of_date FROM riskapp.ref_curve_date WHERE ccy = %s ORDER BY as_of_date", (ccy,)
                    ).as_of_date.tolist()


@st.cache_data(ttl=600, show_spinner=False)
def historical_curve(ccy: str, as_of: dt.date) -> pd.DataFrame:
    return db.query("SELECT tenor_label, tenor_years, zero_rate FROM riskapp.zero_curve_history "
                    "WHERE ccy = %s AND as_of_date = %s ORDER BY tenor_years", (ccy, as_of))


@st.cache_data(ttl=600, show_spinner=False)
def sim_steps() -> pd.DataFrame:
    return db.query("SELECT sim_as_of_date, time_step, sim_date, t_years, days_ahead, n_paths FROM riskapp.ref_sim_step "
                    "WHERE sim_as_of_date = (SELECT max(sim_as_of_date) FROM riskapp.ref_sim_step) ORDER BY time_step")


@st.cache_data(ttl=600, show_spinner=False)
def sim_model(ccy: str, sim_as_of: dt.date) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Initial curve nodes (times, zeros) and HW params the simulation used for `ccy`."""
    c = db.query("SELECT tenor_years, zero_rate FROM riskapp.ref_sim_curve WHERE ccy = %s AND sim_as_of_date = %s "
                 "ORDER BY tenor_years", (ccy, sim_as_of))
    p = currencies().set_index("ccy").loc[ccy]
    return c.tenor_years.to_numpy(float), c.zero_rate.to_numpy(float), float(p.hw_alpha), float(p.hw_sigma)


@st.cache_data(ttl=600, show_spinner="Loading simulated paths...")
def sim_state(ccy: str, sim_as_of: dt.date, n_steps: int, n_paths: int) -> np.ndarray:
    s = db.query("SELECT time_step, path_id, x FROM riskapp.sim_rate_state WHERE ccy = %s AND sim_as_of_date = %s "
                 "ORDER BY time_step, path_id", (ccy, sim_as_of))
    return s.x.to_numpy(float).reshape(n_steps, n_paths)


def tenor_config() -> pd.DataFrame:
    return db.query("SELECT tenor_label, tenor_years, on_curve_grid, is_active, updated_by, updated_at "
                    "FROM riskapp_cfg.tenor_config ORDER BY tenor_years")


def tenor_audit(limit: int = 50) -> pd.DataFrame:
    return db.query("SELECT changed_at, changed_by, action, tenor_label, old_tenor_label, new_tenor_label, "
                    "old_is_active, new_is_active FROM riskapp_cfg.tenor_config_audit "
                    "ORDER BY change_id DESC LIMIT %s", (limit,))


def pct(v):
    return np.asarray(v, dtype=float) * 100.0


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
st.title("Yield curves and simulated rates")
with st.sidebar:
    st.caption("Signed in as")
    st.write(USER or "unknown user")
    st.caption("Tenor editing: " + ("allowed" if CAN_EDIT else "read-only"))
    if st.button("Refresh data"):
        st.cache_data.clear()
        st.rerun()

try:
    CUR = currencies()
    TEN = tenor_config()
except Exception as e:  # surface connection / permission problems clearly
    st.error(f"Could not read from Lakebase: {e}")
    st.stop()

ACTIVE = TEN[TEN.is_active].reset_index(drop=True)
ccy_label = {r.ccy: f"{r.ccy} - {r.ccy_name}" for r in CUR.itertuples()}

tab_hist, tab_cmp, tab_spr, tab_sim, tab_ten, tab_wi, tab_forecast = st.tabs(
    ["Historical curves", "Compare curves", "Currency spreads", "Simulated rates", "Tenors", "Trade what-if", "VaR Forecast"])

# ---------------------------------------------------------------------------
# R1. Historical curves
# ---------------------------------------------------------------------------
with tab_hist:
    c1, c2, c3 = st.columns([1, 1, 1])
    ccy = c1.selectbox("Curve", CUR.ccy.tolist(), format_func=ccy_label.get, key="h_ccy")
    dates = curve_dates(ccy)
    picked = c2.date_input("As-of date", value=dates[-1], min_value=dates[0], max_value=dates[-1], key="h_date")
    compare = c3.date_input("Compare with (optional)", value=None, min_value=dates[0], max_value=dates[-1], key="h_cmp")

    def snap(d: dt.date) -> dt.date:
        """Latest date with a curve on or before d (curves exist on business days only)."""
        i = int(np.searchsorted(np.array(dates, dtype="datetime64[D]"), np.datetime64(d, "D"), side="right")) - 1
        return dates[max(i, 0)]

    fig = go.Figure()
    shown = []
    for d, dash in [(picked, "solid"), (compare, "dot")]:
        if d is None:
            continue
        d0 = snap(d)
        hc = historical_curve(ccy, d0)
        if hc.empty:
            continue
        zc = ZeroCurve(hc.tenor_years, hc.zero_rate)
        grid = set(hc.tenor_label)
        extra = ACTIVE[~ACTIVE.tenor_label.isin(grid)]
        name = d0.isoformat()
        fig.add_trace(go.Scatter(x=hc.tenor_years, y=pct(hc.zero_rate), mode="lines+markers", name=f"{name} (bootstrapped)",
                                 line=dict(dash=dash), text=hc.tenor_label,
                                 hovertemplate="%{text}: %{y:.3f}%<extra>" + name + "</extra>"))
        if len(extra):
            fig.add_trace(go.Scatter(x=extra.tenor_years, y=pct(zc.zero(extra.tenor_years)), mode="markers",
                                     name=f"{name} (interpolated tenors)", text=extra.tenor_label,
                                     marker=dict(symbol="circle-open", size=10),
                                     hovertemplate="%{text}: %{y:.3f}% (interpolated)<extra>" + name + "</extra>"))
        rows = pd.concat([hc.assign(source="bootstrapped"),
                          pd.DataFrame({"tenor_label": extra.tenor_label, "tenor_years": extra.tenor_years,
                                        "zero_rate": zc.zero(extra.tenor_years), "source": "interpolated"})])
        shown.append(rows.sort_values("tenor_years").assign(as_of_date=d0))
        if d0 != d:
            st.caption(f"No curve on {d.isoformat()}; showing the previous business day, {name}.")
    fig.update_layout(xaxis_title="Maturity (years)", yaxis_title="Zero rate (%)", height=480,
                      legend=dict(orientation="h", y=-0.2), margin=dict(t=30))
    st.plotly_chart(fig, use_container_width=True)
    if shown:
        tbl = pd.concat(shown)
        tbl["zero_rate_pct"] = pct(tbl.zero_rate).round(4)
        st.dataframe(tbl.pivot_table(index=["tenor_years", "tenor_label", "source"], columns="as_of_date",
                                     values="zero_rate_pct").reset_index(), hide_index=True, use_container_width=True)
    st.caption("Zero rates are continuously compounded, ACT/365. Tenors off the bootstrap grid use linear interpolation "
               "in zero rate, the same rule as the simulation's initial curve.")

# ---------------------------------------------------------------------------
# R1b. Compare curves across dates, highlighting the largest moves
# ---------------------------------------------------------------------------
UP, DOWN, GREY = "#2375A8", "#C89930", "#B8BCC2"     # colour-blind-safe rise / fall


def curve_on_tenors(ccy: str, d: dt.date, tenors: pd.DataFrame) -> pd.Series:
    """Zero rates for `tenors` on date d: bootstrapped points as stored, other tenors interpolated."""
    hc = historical_curve(ccy, d)
    exact = dict(zip(hc.tenor_label, hc.zero_rate))
    zc = ZeroCurve(hc.tenor_years, hc.zero_rate)
    return pd.Series([exact.get(l, float(zc.zero(y))) for l, y in zip(tenors.tenor_label, tenors.tenor_years)],
                     index=tenors.tenor_label)


with tab_cmp:
    ccys = CUR.ccy.tolist()
    c1, c2, c3, c4 = st.columns([1, 2.2, 1.2, 1])
    cc = c1.selectbox("Curve", ccys, index=ccys.index("USD") if "USD" in ccys else 0,
                      format_func=ccy_label.get, key="c_ccy")
    newest_first = curve_dates(cc)[::-1]
    # default: the latest market (not simulated) curve versus the business day before it
    sel = c2.multiselect("Dates (two or more)", newest_first, default=newest_first[:2], key=f"c_dates_{cc}",
                         format_func=lambda d: d.isoformat())
    sel = sorted(sel, reverse=True)
    if len(sel) < 2:
        st.info("Pick at least two dates to compare.")
    else:
        latest = sel[0]
        base = c3.selectbox(f"Measure changes in {latest} from", sel[1:], index=0, key=f"c_base_{cc}",
                            format_func=lambda d: d.isoformat())
        top_n = c4.slider("Highlight largest moves", 1, 8, 3, key="c_top")

        hc0 = historical_curve(cc, latest)
        grid = set(hc0.tenor_label)
        tenors = pd.concat([hc0[["tenor_label", "tenor_years"]],
                            ACTIVE[~ACTIVE.tenor_label.isin(grid)][["tenor_label", "tenor_years"]]]
                           ).sort_values("tenor_years").reset_index(drop=True)
        tenors["source"] = np.where(tenors.tenor_label.isin(grid), "bootstrapped", "interpolated")
        rates = pd.DataFrame({d: curve_on_tenors(cc, d, tenors) for d in sel})      # tenor_label x date
        chg_bp = (rates[latest] - rates[base]) * 1e4
        top = chg_bp.abs().sort_values(ascending=False).index[:top_n]

        k1, k2, k3, k4 = st.columns(4)
        biggest = chg_bp.abs().idxmax()
        k1.metric("Largest move", f"{biggest}: {chg_bp[biggest]:+.1f} bp")
        k2.metric("Average move", f"{chg_bp.mean():+.1f} bp")
        sl_new = (rates[latest].get("10Y", np.nan) - rates[latest].get("2Y", np.nan)) * 1e4
        sl_old = (rates[base].get("10Y", np.nan) - rates[base].get("2Y", np.nan)) * 1e4
        k3.metric("2s10s slope", f"{sl_new:.1f} bp", f"{sl_new - sl_old:+.1f} bp")
        k4.metric("Tenors rising / falling", f"{int((chg_bp > 0).sum())} / {int((chg_bp < 0).sum())}")

        fig = go.Figure()
        palette = ["#08141A", "#15AFDD", "#52A870", "#C85070", "#7A5CC2", "#5F6B73"]
        for i, d in enumerate(sel):
            emph = d in (latest, base)
            fig.add_trace(go.Scatter(
                x=tenors.tenor_years, y=pct(rates[d].to_numpy()), mode="lines+markers", name=d.isoformat(),
                text=tenors.tenor_label, line=dict(width=3 if d == latest else 2 if emph else 1,
                                                   dash="solid" if emph else "dot", color=palette[i % len(palette)]),
                marker=dict(size=7 if emph else 4,
                            symbol=["circle" if s == "bootstrapped" else "circle-open" for s in tenors.source]),
                opacity=1.0 if emph else 0.6,
                hovertemplate="%{text}: %{y:.3f}%<extra>" + d.isoformat() + "</extra>"))
        # highlight the largest moves: a bar from the base value to the latest value, labelled in bp
        ty = tenors.set_index("tenor_label").tenor_years
        for lab in top:
            y0, y1, c = pct(rates.at[lab, base]), pct(rates.at[lab, latest]), UP if chg_bp[lab] > 0 else DOWN
            fig.add_trace(go.Scatter(x=[ty[lab], ty[lab]], y=[y0, y1], mode="lines+markers", showlegend=False,
                                     line=dict(color=c, width=6), marker=dict(size=11, color=c, symbol="diamond"),
                                     hoverinfo="skip"))
            fig.add_annotation(x=ty[lab], y=max(y0, y1), text=f"<b>{lab} {chg_bp[lab]:+.1f}bp</b>", showarrow=True,
                               arrowhead=0, ay=-28, font=dict(color=c, size=12), bgcolor="rgba(255,255,255,0.8)")
        fig.update_layout(title=f"{cc} zero curves - largest moves {base} to {latest} highlighted",
                          xaxis_title="Maturity (years)", yaxis_title="Zero rate (%)", height=500, margin=dict(t=50),
                          legend=dict(orientation="h", y=-0.2))
        st.plotly_chart(fig, use_container_width=True)

        bars = go.Figure(go.Bar(
            x=tenors.tenor_label, y=chg_bp.reindex(tenors.tenor_label).to_numpy(),
            marker_color=[(UP if chg_bp[l] > 0 else DOWN) if l in top else GREY for l in tenors.tenor_label],
            text=[f"{chg_bp[l]:+.1f}" for l in tenors.tenor_label], textposition="outside",
            hovertemplate="%{x}: %{y:+.2f} bp<extra></extra>"))
        bars.update_layout(title=f"Change {base} to {latest} by tenor (bp); top {top_n} highlighted", height=360,
                           yaxis_title="Change (bp)", margin=dict(t=50),
                           xaxis=dict(categoryorder="array", categoryarray=tenors.tenor_label.tolist()))
        st.plotly_chart(bars, use_container_width=True)

        tbl = tenors.copy()
        for d in sel:
            tbl[d.isoformat()] = pct(rates[d].reindex(tenors.tenor_label).to_numpy()).round(4)
        tbl["change_bp"] = chg_bp.reindex(tenors.tenor_label).round(2).to_numpy()
        tbl["rank_by_size"] = chg_bp.abs().rank(ascending=False, method="min").reindex(tenors.tenor_label).astype(int).to_numpy()
        st.dataframe(tbl, hide_index=True, use_container_width=True)
        st.caption(f"Rates are zero rates in %; change_bp is {latest} minus {base}. {latest} is the latest date with "
                   "market data (not simulated). Hollow markers are tenors off the bootstrap grid, interpolated linearly "
                   "in zero rate.")

# ---------------------------------------------------------------------------
# R1c. Curve spreads between two currencies
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def tenor_history(tenor: str, ccy_a: str, ccy_b: str) -> pd.DataFrame:
    """Zero rate history of one bootstrap-grid tenor for two currencies, one row per common date."""
    h = db.query("SELECT as_of_date, ccy, zero_rate FROM riskapp.zero_curve_history "
                 "WHERE tenor_label = %s AND ccy IN (%s, %s) ORDER BY as_of_date", (tenor, ccy_a, ccy_b))
    return h.pivot(index="as_of_date", columns="ccy", values="zero_rate").dropna()


@st.cache_data(ttl=600, show_spinner=False)
def proxy_tenors(ccy: str, d: dt.date) -> list[str]:
    return db.query("SELECT tenor_label FROM riskapp.zero_curve_history WHERE ccy = %s AND as_of_date = %s AND is_proxy "
                    "ORDER BY tenor_years", (ccy, d)).tenor_label.tolist()


with tab_spr:
    ccys = CUR.ccy.tolist()
    c1, c2, c3, c4 = st.columns(4)
    ca = c1.selectbox("Currency A", ccys, index=ccys.index("USD") if "USD" in ccys else 0,
                      format_func=ccy_label.get, key="sp_a")
    cb = c2.selectbox("Currency B", [c for c in ccys if c != ca],
                      index=max(0, [c for c in ccys if c != ca].index("EUR")) if "EUR" in ccys and ca != "EUR" else 0,
                      format_func=ccy_label.get, key="sp_b")
    common = sorted(set(curve_dates(ca)) & set(curve_dates(cb)), reverse=True)
    d1 = c3.selectbox("Date", common, index=0, key=f"sp_d_{ca}_{cb}", format_func=lambda d: d.isoformat())
    d0 = c4.selectbox("Change since (optional)", [None] + [d for d in common if d < d1], index=0, key=f"sp_d0_{ca}_{cb}",
                      format_func=lambda d: "none" if d is None else d.isoformat())

    hca = historical_curve(ca, d1)
    grid = set(hca.tenor_label)
    tenors = pd.concat([hca[["tenor_label", "tenor_years"]],
                        ACTIVE[~ACTIVE.tenor_label.isin(grid)][["tenor_label", "tenor_years"]]]
                       ).sort_values("tenor_years").reset_index(drop=True)
    tenors["source"] = np.where(tenors.tenor_label.isin(grid), "bootstrapped", "interpolated")
    ra, rb = curve_on_tenors(ca, d1, tenors), curve_on_tenors(cb, d1, tenors)
    spread = (ra - rb) * 1e4                                                      # bp, A minus B
    spread0 = None if d0 is None else (curve_on_tenors(ca, d0, tenors) - curve_on_tenors(cb, d0, tenors)) * 1e4

    def sp(lab):
        return spread.get(lab, np.nan)

    k1, k2, k3, k4 = st.columns(4)
    for col, lab in [(k1, "2Y"), (k2, "10Y"), (k3, "30Y")]:
        delta = None if spread0 is None else f"{sp(lab) - spread0.get(lab, np.nan):+.1f} bp since {d0}"
        col.metric(f"{lab} spread ({ca} - {cb})", f"{sp(lab):+.1f} bp", delta)
    slope = sp("10Y") - sp("2Y")
    k4.metric("Spread slope (10Y - 2Y)", f"{slope:+.1f} bp",
              None if spread0 is None else f"{slope - (spread0.get('10Y', np.nan) - spread0.get('2Y', np.nan)):+.1f} bp")

    p1, p2 = st.columns(2)
    curves = go.Figure()
    for lab_ccy, r, col in [(ca, ra, "#2375A8"), (cb, rb, "#C85070")]:
        curves.add_trace(go.Scatter(
            x=tenors.tenor_years, y=pct(r.to_numpy()), mode="lines+markers", name=lab_ccy, text=tenors.tenor_label,
            line=dict(color=col, width=2.5),
            marker=dict(size=7, symbol=["circle" if s == "bootstrapped" else "circle-open" for s in tenors.source]),
            hovertemplate="%{text}: %{y:.3f}%<extra>" + lab_ccy + "</extra>"))
    curves.update_layout(title=f"{ca} and {cb} zero curves on {d1}", xaxis_title="Maturity (years)",
                         yaxis_title="Zero rate (%)", height=430, margin=dict(t=50), legend=dict(orientation="h", y=-0.2))
    p1.plotly_chart(curves, use_container_width=True)

    bars = go.Figure(go.Bar(x=tenors.tenor_label, y=spread.reindex(tenors.tenor_label).to_numpy(), name=f"{d1}",
                            marker_color=["#2375A8" if v >= 0 else "#C89930" for v in spread.reindex(tenors.tenor_label)],
                            text=[f"{v:+.0f}" for v in spread.reindex(tenors.tenor_label)], textposition="outside",
                            hovertemplate="%{x}: %{y:+.1f} bp<extra>" + d1.isoformat() + "</extra>"))
    if spread0 is not None:
        bars.add_trace(go.Scatter(x=tenors.tenor_label, y=spread0.reindex(tenors.tenor_label).to_numpy(), mode="markers",
                                  name=f"{d0}", marker=dict(symbol="line-ew-open", size=22, line=dict(width=3, color="#08141A")),
                                  hovertemplate="%{x}: %{y:+.1f} bp<extra>" + d0.isoformat() + "</extra>"))
    bars.update_layout(title=f"Spread {ca} - {cb} by tenor (bp)", yaxis_title="Spread (bp)", height=430, margin=dict(t=50),
                       xaxis=dict(categoryorder="array", categoryarray=tenors.tenor_label.tolist()),
                       legend=dict(orientation="h", y=-0.2), showlegend=spread0 is not None)
    p2.plotly_chart(bars, use_container_width=True)

    # spread history for one tenor (bootstrap-grid tenors have a stored history)
    grid_labels = hca.tenor_label.tolist()
    t1, t2 = st.columns([1, 3])
    ts_tenor = t1.selectbox("Spread history for tenor", grid_labels,
                            index=grid_labels.index("10Y") if "10Y" in grid_labels else 0, key="sp_ts_tenor")
    hist_ab = tenor_history(ts_tenor, ca, cb)
    s = (hist_ab[ca] - hist_ab[cb]) * 1e4
    last_year = s[s.index > d1 - dt.timedelta(days=365)]
    last_year = last_year[last_year.index <= d1]
    now = float(s.get(d1, np.nan))
    t1.metric("On selected date", f"{now:+.1f} bp")
    if len(last_year):
        t1.metric("1-year average", f"{last_year.mean():+.1f} bp")
        t1.metric("1-year range", f"{last_year.min():+.0f} to {last_year.max():+.0f} bp")
        t1.metric("Percentile in last year", f"{(last_year <= now).mean() * 100:.0f}%")
    ts = go.Figure(go.Scatter(x=s.index, y=s.to_numpy(), mode="lines", line=dict(color="#2375A8", width=1.5),
                              name=f"{ts_tenor} spread", hovertemplate="%{x}: %{y:+.1f} bp<extra></extra>"))
    ts.add_hline(y=0, line_color="#B8BCC2")
    ts.add_vline(x=pd.Timestamp(d1).to_pydatetime(), line_dash="dot")
    ts.update_layout(title=f"{ts_tenor} spread {ca} - {cb} over time (bp)", yaxis_title="Spread (bp)", height=380,
                     margin=dict(t=50))
    t2.plotly_chart(ts, use_container_width=True)

    tbl = tenors.copy()
    tbl[f"{ca} %"] = pct(ra.reindex(tenors.tenor_label).to_numpy()).round(4)
    tbl[f"{cb} %"] = pct(rb.reindex(tenors.tenor_label).to_numpy()).round(4)
    tbl["spread_bp"] = spread.reindex(tenors.tenor_label).round(2).to_numpy()
    if spread0 is not None:
        tbl[f"spread_bp_{d0}"] = spread0.reindex(tenors.tenor_label).round(2).to_numpy()
        tbl["change_bp"] = (tbl.spread_bp - tbl[f"spread_bp_{d0}"]).round(2)
    st.dataframe(tbl, hide_index=True, use_container_width=True)
    proxies = {c: proxy_tenors(c, d1) for c in (ca, cb)}
    note = "; ".join(f"{c}: " + ("whole curve" if set(p) >= grid else ", ".join(p)) for c, p in proxies.items() if p)
    st.caption(f"Spread = {ca} zero rate minus {cb} zero rate, in basis points. Hollow markers are tenors off the bootstrap "
               "grid, interpolated linearly in zero rate. Dates are those with market curves for both currencies."
               + (f" Proxy-based points on {d1} (OECD monthly 3M / 10Y inputs, so spreads at these tenors are "
                  f"less precise): {note}." if note else ""))

# ---------------------------------------------------------------------------
# R2. Simulated rates
# ---------------------------------------------------------------------------
with tab_sim:
    steps = sim_steps()
    sim_as_of = steps.sim_as_of_date.iloc[0]
    n_steps, n_paths = len(steps), int(steps.n_paths.iloc[0])
    sim_ccys = CUR[CUR.is_simulated].ccy.tolist()

    c1, c2, c3 = st.columns([1, 1, 1.4])
    sccy = c1.selectbox("Currency", sim_ccys, format_func=ccy_label.get, key="s_ccy")
    rate_opts = ["SHORT"] + ACTIVE.tenor_label.tolist()
    rate = c2.selectbox("Rate", rate_opts, index=rate_opts.index("10Y") if "10Y" in rate_opts else 0,
                        format_func=lambda r: "Short rate" if r == "SHORT" else f"{r} zero rate", key="s_rate")
    step_label = {r.time_step: f"{r.sim_date} (+{r.t_years:.2f}y, step {r.time_step})" for r in steps.itertuples()}
    default_step = int(np.argmin(np.abs(steps.t_years.to_numpy() - 1.0)))
    step = c3.selectbox("Simulation date", steps.time_step.tolist(), index=default_step,
                        format_func=step_label.get, key="s_step")

    times, zeros, alpha, sigma = sim_model(sccy, sim_as_of)
    hw = HW1F(ZeroCurve(times, zeros), alpha, sigma)
    X = sim_state(sccy, sim_as_of, n_steps, n_paths)
    tau = None if rate == "SHORT" else float(ACTIVE.set_index("tenor_label").tenor_years[rate])
    R = simulated_rate(hw, steps.t_years.to_numpy(float), X, tau)        # (steps, paths)
    v = R[step]
    on_grid = rate == "SHORT" or bool(ACTIVE.set_index("tenor_label").on_curve_grid[rate])

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Today", f"{pct(R[0].mean()):.3f}%")
    k2.metric("Mean", f"{pct(v.mean()):.3f}%", f"{pct(v.mean() - R[0].mean()):+.3f}% vs today")
    k3.metric("Std dev", f"{pct(v.std(ddof=1)):.3f}%")
    k4.metric("5% - 95%", f"{pct(np.percentile(v, 5)):.2f}% to {pct(np.percentile(v, 95)):.2f}%")

    h1, h2 = st.columns([1, 1])
    hist = go.Figure(go.Histogram(x=pct(v), nbinsx=50, marker_line_width=0.5))
    for p, dash in [(5, "dot"), (50, "dash"), (95, "dot")]:
        hist.add_vline(x=float(pct(np.percentile(v, p))), line_dash=dash, annotation_text=f"P{p}")
    hist.update_layout(title=f"Distribution on {steps.sim_date[step]} ({n_paths:,} paths)", height=420,
                       xaxis_title="Rate (%)", yaxis_title="Paths", margin=dict(t=50), bargap=0.02)
    h1.plotly_chart(hist, use_container_width=True)

    q = np.percentile(R, [5, 25, 50, 75, 95], axis=1)
    fan = go.Figure()
    x = steps.sim_date
    fan.add_trace(go.Scatter(x=x, y=pct(q[4]), line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fan.add_trace(go.Scatter(x=x, y=pct(q[0]), fill="tonexty", line=dict(width=0), name="P5-P95",
                             fillcolor="rgba(35,117,168,0.18)"))
    fan.add_trace(go.Scatter(x=x, y=pct(q[3]), line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fan.add_trace(go.Scatter(x=x, y=pct(q[1]), fill="tonexty", line=dict(width=0), name="P25-P75",
                             fillcolor="rgba(35,117,168,0.38)"))
    fan.add_trace(go.Scatter(x=x, y=pct(q[2]), name="Median", line=dict(color="#2375A8")))
    fan.add_trace(go.Scatter(x=x, y=pct(R.mean(axis=1)), name="Mean", line=dict(color="#C85070", dash="dash")))
    fan.add_vline(x=pd.Timestamp(steps.sim_date[step]).to_pydatetime(), line_dash="dot")
    fan.update_layout(title="Percentiles across all simulation dates", height=420, yaxis_title="Rate (%)",
                      margin=dict(t=50), legend=dict(orientation="h", y=-0.2))
    h2.plotly_chart(fan, use_container_width=True)

    stats = pd.DataFrame({"statistic": ["mean", "std dev"] + [f"P{p}" for p in PCTS],
                          "rate_pct": np.round(pct([v.mean(), v.std(ddof=1)] + list(np.percentile(v, PCTS))), 4)})
    st.dataframe(stats.T, use_container_width=True)
    st.caption(
        f"Simulation as of {sim_as_of}: Hull-White 1F, a = {alpha:.4f}, sigma = {sigma:.5f}. "
        + ("The short rate is x(t) + phi(t)." if rate == "SHORT" else
           f"The {rate} zero rate is read off the curve rebuilt on each path from the simulated state x(t)"
           + ("." if on_grid else "; this tenor is not on the bootstrap grid.")))

# ---------------------------------------------------------------------------
# R3. Tenor list
# ---------------------------------------------------------------------------
def friendly(err: Exception) -> str:
    msg = str(err).splitlines()[0]
    if "tenor_years_unique" in msg:
        return "Another tenor already has that maturity."
    if "tenor_config_pkey" in msg:
        return "That tenor label already exists (it may be removed - restore it instead)."
    if "tenor_label_matches_years" in msg or "tenor_label_check" in msg:
        return "The label must be a whole number of months (M) or years (Y), e.g. 18M or 25Y."
    if "tenor_years_check" in msg:
        return "Maturity must be greater than 0 and at most 50 years."
    return msg


def apply_change(sql: str, params: tuple, ok: str):
    try:
        n = db.execute_as(USER, sql, params)
        if n == 0:
            st.warning("Nothing changed - the tenor may have been edited by someone else. Refresh and try again.")
        else:
            st.session_state["tenor_msg"] = ok
            st.rerun()
    except psycopg.Error as e:
        st.error(friendly(e))


with tab_ten:
    if msg := st.session_state.pop("tenor_msg", None):
        st.success(msg)
    st.write(f"{len(ACTIVE)} active tenors. The {int(TEN.on_curve_grid.sum())} bootstrap-grid tenors are curve inputs "
             "and are locked; other tenors can be added, renamed, removed and restored. Changes apply immediately "
             "to both the historical and simulated views.")
    view = TEN.assign(status=np.where(TEN.is_active, "active", "removed"),
                      type=np.where(TEN.on_curve_grid, "bootstrap grid (locked)", "user tenor"))
    st.dataframe(view[["tenor_label", "tenor_years", "type", "status", "updated_by", "updated_at"]],
                 hide_index=True, use_container_width=True)

    if not CAN_EDIT:
        st.info("You can view the tenor list. Ask an app editor to add, change or remove tenors.")
    else:
        editable = TEN[~TEN.on_curve_grid & TEN.is_active].tenor_label.tolist()
        removed = TEN[~TEN.is_active].tenor_label.tolist()
        a, e, r, s = st.columns(4)
        with a.form("add_tenor", clear_on_submit=True):
            st.subheader("Add")
            new = st.text_input("New tenor", placeholder="e.g. 40Y or 30M")
            if st.form_submit_button("Add tenor") and new:
                try:
                    lab = new.strip().upper()
                    apply_change("INSERT INTO riskapp_cfg.tenor_config (tenor_label, tenor_years) VALUES (%s, %s)",
                                 (lab, tenor_years(lab)), f"Added {lab}.")
                except ValueError as ve:
                    st.error(str(ve))
        with e.form("edit_tenor"):
            st.subheader("Edit")
            old = st.selectbox("Tenor", editable, key="edit_old") if editable else None
            new = st.text_input("Change to", placeholder="e.g. 26Y")
            if st.form_submit_button("Save change", disabled=not editable) and old and new:
                try:
                    lab = new.strip().upper()
                    apply_change("UPDATE riskapp_cfg.tenor_config SET tenor_label = %s, tenor_years = %s "
                                 "WHERE tenor_label = %s AND is_active AND NOT on_curve_grid",
                                 (lab, tenor_years(lab), old), f"Changed {old} to {lab}.")
                except ValueError as ve:
                    st.error(str(ve))
        with r.form("remove_tenor"):
            st.subheader("Remove")
            rem = st.selectbox("Tenor", editable, key="rem") if editable else None
            if st.form_submit_button("Remove tenor", disabled=not editable) and rem:
                apply_change("UPDATE riskapp_cfg.tenor_config SET is_active = false "
                             "WHERE tenor_label = %s AND is_active AND NOT on_curve_grid", (rem,), f"Removed {rem}.")
        with s.form("restore_tenor"):
            st.subheader("Restore")
            res = st.selectbox("Tenor", removed, key="res") if removed else None
            if st.form_submit_button("Restore tenor", disabled=not removed) and res:
                apply_change("UPDATE riskapp_cfg.tenor_config SET is_active = true WHERE tenor_label = %s AND NOT is_active",
                             (res,), f"Restored {res}.")

    with st.expander("Change history"):
        st.dataframe(tenor_audit(), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# R4. Trade what-if (pricing, sensitivities, VaR / ES and exposure impact)
# ---------------------------------------------------------------------------
with tab_wi:
    try:
        import whatif_tab
    except Exception as e:  # keep the other tabs usable if the risk library cannot load
        st.error(f"The what-if tab could not load: {e}")
    else:
        whatif_tab.render(USER)

# ---------------------------------------------------------------------------
# R5. VaR Forecast: compare Monte Carlo VaR vs ML 10-day ahead forecast
# ---------------------------------------------------------------------------
CATALOG = os.environ.get("DATABRICKS_CATALOG", "serverless_stable_7s0j2u_catalog")
SCHEMA  = os.environ.get("DATABRICKS_SCHEMA",  "market")


@st.cache_data(ttl=1800, show_spinner=False)
def load_forecast_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load latest MC VaR actuals and ML forecast via SQL warehouse."""
    try:
        import wh
        # Latest available MC VaR (all books + each book, last 60 days)
        mc_df = wh.wh_query(f"""
            SELECT as_of_date, book_id, var_95, expected_shortfall_95
            FROM {CATALOG}.{SCHEMA}.gold_book_var
            WHERE as_of_date >= date_sub(current_date(), 90)
            ORDER BY book_id, as_of_date
        """)
        # Latest forecast batch (most recent forecast_date)
        fc_df = wh.wh_query(f"""
            SELECT as_of_date, book_id, horizon_days, var_95_forecast, model_version
            FROM {CATALOG}.{SCHEMA}.gold_var_forecast
            WHERE forecast_date = (SELECT max(forecast_date) FROM {CATALOG}.{SCHEMA}.gold_var_forecast)
            ORDER BY book_id, as_of_date
        """)
        mc_df["as_of_date"] = pd.to_datetime(mc_df["as_of_date"])
        fc_df["as_of_date"] = pd.to_datetime(fc_df["as_of_date"])
        for col in ["var_95", "expected_shortfall_95"]:
            if col in mc_df:
                mc_df[col] = pd.to_numeric(mc_df[col], errors="coerce")
        fc_df["var_95_forecast"] = pd.to_numeric(fc_df["var_95_forecast"], errors="coerce")
        return mc_df, fc_df
    except Exception as e:
        return pd.DataFrame(), pd.DataFrame()


with tab_forecast:
    st.caption(
        "Compares the 95% 1-day VaR from the Monte Carlo simulation (actual, computed by notebook 13) "
        "with the 10-business-day ML forecast (GBR model trained by notebook 16, monitored weekly by notebook 17). "
        "The forecast is regenerated each Monday morning."
    )
    try:
        mc_raw, fc_raw = load_forecast_data()
    except Exception as _e:
        st.error(f"Could not load forecast data: {_e}")
        st.stop()

    if mc_raw.empty and fc_raw.empty:
        st.info(
            "No VaR forecast data found. Run notebook 16_var_forecast_automl to train the model and generate "
            "the first forecast, then refresh this page."
        )
    else:
        # Book / TOTAL selector
        all_books = sorted(mc_raw["book_id"].unique().tolist()) if not mc_raw.empty else []
        # Compute TOTAL if not already in the table
        if "TOTAL" not in all_books and not mc_raw.empty:
            total_mc = (
                mc_raw.groupby("as_of_date")[["var_95", "expected_shortfall_95"]]
                .sum().reset_index().assign(book_id="TOTAL")
            )
            mc_all = pd.concat([mc_raw, total_mc], ignore_index=True)
        else:
            mc_all = mc_raw
        book_opts = ["TOTAL"] + [b for b in all_books if b != "TOTAL"]
        sel_book = st.selectbox("Book", book_opts, key="fc_book")

        mc_book = mc_all[mc_all["book_id"] == sel_book].sort_values("as_of_date")
        fc_book = fc_raw[fc_raw["book_id"] == sel_book].sort_values("as_of_date") if not fc_raw.empty else pd.DataFrame()

        # Key metrics row
        m1, m2, m3, m4 = st.columns(4)
        if not mc_book.empty:
            latest_mc = float(mc_book.iloc[-1]["var_95"])
            m1.metric("Latest MC VaR (95%, 1-day)", f"${latest_mc:,.0f}")
        if not fc_book.empty:
            nearest = fc_book.iloc[0]
            m2.metric("ML Forecast (next 1 bday)",   f"${float(nearest['var_95_forecast']):,.0f}")
            furthest = fc_book.iloc[-1]
            m3.metric("ML Forecast (next 10 bdays)",  f"${float(furthest['var_95_forecast']):,.0f}")
            if not mc_book.empty:
                bias = float(nearest["var_95_forecast"]) - float(mc_book.iloc[-1]["var_95"])
                m4.metric("Model vs Latest MC", f"${bias:+,.0f}",
                          delta_color="normal" if abs(bias) / max(latest_mc, 1) < 0.15 else "inverse")

        # Combined chart: MC actuals + ML forecast
        fig = go.Figure()
        if not mc_book.empty:
            fig.add_trace(go.Scatter(
                x=mc_book["as_of_date"], y=mc_book["var_95"],
                mode="lines", name="MC VaR (actual)",
                line=dict(color="#2375A8", width=2),
                hovertemplate="%{x|%Y-%m-%d}: $%{y:,.0f}<extra>MC VaR</extra>"
            ))
        if not fc_book.empty:
            # Stitch from the last MC date so the chart connects
            last_mc_date = mc_book["as_of_date"].max() if not mc_book.empty else None
            last_mc_val  = float(mc_book.iloc[-1]["var_95"]) if not mc_book.empty else None
            fc_x = fc_book["as_of_date"].tolist()
            fc_y = fc_book["var_95_forecast"].tolist()
            if last_mc_date is not None:
                fc_x = [last_mc_date] + fc_x
                fc_y = [last_mc_val]  + fc_y
            fig.add_trace(go.Scatter(
                x=fc_x, y=fc_y,
                mode="lines+markers", name="ML Forecast (10-day)",
                line=dict(color="#C85070", width=2, dash="dash"),
                marker=dict(size=7),
                hovertemplate="%{x|%Y-%m-%d}: $%{y:,.0f}<extra>ML Forecast</extra>"
            ))

        fig.update_layout(
            title=f"{sel_book} — Monte Carlo VaR (actual) vs ML 10-day Forecast",
            xaxis_title="Date", yaxis_title="VaR 95% ($)",
            height=440, margin=dict(t=50),
            legend=dict(orientation="h", y=-0.2)
        )
        st.plotly_chart(fig, use_container_width=True)

        # Forecast table
        if not fc_book.empty:
            fc_tbl = fc_book[["as_of_date", "horizon_days", "var_95_forecast", "model_version"]].copy()
            fc_tbl["var_95_forecast"] = fc_tbl["var_95_forecast"].round(0)
            fc_tbl.columns = ["Forecast Date", "Horizon (bdays)", "VaR 95% Forecast ($)", "Model Version"]
            with st.expander("Forecast detail"):
                st.dataframe(fc_tbl, hide_index=True, use_container_width=True)

        st.caption(
            f"MC VaR: 95th-percentile loss from {mc_raw['as_of_date'].nunique() if not mc_raw.empty else 0} "
            "simulation paths across all books (computed daily by notebook 13_calc_exposure). "
            "ML Forecast: GBR model trained on historical book VaR time series with lag/rolling features, "
            "registered in Unity Catalog as var_forecast_model @champion. "
            "Model performance monitored weekly; retrains automatically if SMAPE degrades >10%."
        )
