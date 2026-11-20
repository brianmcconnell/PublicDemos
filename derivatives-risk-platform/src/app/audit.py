"""Lakehouse audit of what-if runs: one row per run in market.whatif_run_audit (see src/sql/whatif_run_audit.sql).

Headline numbers only, the trade as priced, when it was calculated and who ran it. Written by the app's service
principal through a SQL warehouse (app resource "sql-warehouse") with a parameterised INSERT. run_by is masked
in Unity Catalog for everyone outside the workspace admins group.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import uuid

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState

def _default_table() -> str:
    """<catalog>.<schema>.whatif_run_audit from SIM_CUBE_DIR=/Volumes/<catalog>/<schema>/sim_cubes (the app's volume
    resource), so the table follows the environment the app is deployed into."""
    parts = os.environ.get("SIM_CUBE_DIR", "").strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "Volumes":
        return f"{parts[1]}.{parts[2]}.whatif_run_audit"
    return "serverless_stable_7s0j2u_catalog.market.whatif_run_audit"


TABLE = os.environ.get("WHATIF_AUDIT_TABLE") or _default_table()
WAREHOUSE = os.environ.get("WHATIF_AUDIT_WAREHOUSE", "")
APP_NAME = os.environ.get("DATABRICKS_APP_NAME", "risk-curves-app")
_w = WorkspaceClient()

# column -> SQL type, in table order (audit_id and logged_at are set in the statement)
COLUMNS = {
    "calculated_at": "TIMESTAMP", "run_by": "STRING", "app_name": "STRING", "lakebase_run_id": "BIGINT",
    "whatif_trade_id": "STRING", "trade_label": "STRING", "valuation_date": "DATE", "sim_cube_file": "STRING",
    "n_paths": "INT", "product": "STRING", "book_id": "STRING", "counterparty_id": "STRING",
    "netting_set_id": "STRING", "collateral_id": "STRING", "direction": "STRING", "ccy": "STRING",
    "ccy_2": "STRING", "underlying": "STRING", "notional": "DOUBLE", "rate_or_strike": "DOUBLE",
    "start_date": "DATE", "maturity_date": "DATE", "trade_details": "STRING", "mtm_usd": "DOUBLE",
    "trade_var_1d": "DOUBLE", "trade_es_1d": "DOUBLE",
    "book_var_1d_before": "DOUBLE", "book_var_1d_after": "DOUBLE", "book_es_1d_before": "DOUBLE",
    "book_es_1d_after": "DOUBLE", "book_var_10d_before": "DOUBLE", "book_var_10d_after": "DOUBLE",
    "book_es_10d_before": "DOUBLE", "book_es_10d_after": "DOUBLE", "firm_var_1d_before": "DOUBLE",
    "firm_var_1d_after": "DOUBLE", "firm_es_1d_before": "DOUBLE", "firm_es_1d_after": "DOUBLE",
    "cpty_netting_key": "STRING", "cpty_peak_pfe_before": "DOUBLE", "cpty_peak_pfe_after": "DOUBLE",
    "cpty_peak_ee_before": "DOUBLE", "cpty_peak_ee_after": "DOUBLE", "cpty_epe_1y_before": "DOUBLE",
    "cpty_epe_1y_after": "DOUBLE", "trade_peak_pfe": "DOUBLE", "compute_seconds": "DOUBLE",
}
INSERT = (f"INSERT INTO {TABLE} (audit_id, logged_at, {', '.join(COLUMNS)}) VALUES (:audit_id, current_timestamp(), "
          + ", ".join(f"CAST(:{c} AS {t})" for c, t in COLUMNS.items()) + ")")


def _clean(v):
    """JSON/SQL-safe scalar: NaN/inf -> None, numpy scalars -> python, dates -> ISO."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if hasattr(v, "isoformat"):            # pandas Timestamp
        return v.isoformat()
    return v


def row(*, user, trade, res, mtm, trade_id, label, lakebase_run_id, as_of, cube_file, n_paths,
        calculated_at, compute_seconds) -> dict:
    """Audit row from a what-if run (trade = the 1-row priced trade frame, res = whatif.impact output)."""
    t = {k: _clean(v) for k, v in trade.iloc[0].to_dict().items()}
    p = t["product"]
    b, f, cp, s = res["book"], res["firm"], res["counterparty"], res["trade"]
    rate = {"fxfwd": t.get("forward_rate"), "euro_opt": t.get("strike"), "amer_opt": t.get("strike")}.get(
        p, t.get("fixed_rate"))
    return {
        "calculated_at": calculated_at.isoformat(), "run_by": user, "app_name": APP_NAME,
        "lakebase_run_id": lakebase_run_id, "whatif_trade_id": trade_id, "trade_label": label or None,
        "valuation_date": str(as_of), "sim_cube_file": cube_file, "n_paths": n_paths, "product": p,
        "book_id": t["book_id"], "counterparty_id": t["counterparty_id"], "netting_set_id": t.get("netting_set_id"),
        "collateral_id": t.get("collateral_id"), "direction": t.get("direction"), "ccy": t.get("ccy"),
        "ccy_2": t.get("ccy_2"), "underlying": t.get("underlying"), "notional": t.get("notional"),
        "rate_or_strike": rate, "start_date": (t.get("start_date") or None) and str(t["start_date"])[:10],
        "maturity_date": (t.get("maturity_date") or None) and str(t["maturity_date"])[:10],
        "trade_details": json.dumps(t, sort_keys=True, default=str), "mtm_usd": mtm,
        "trade_var_1d": s["var_1-day"], "trade_es_1d": s["es_1-day"],
        "book_var_1d_before": b["before"]["var_1-day"], "book_var_1d_after": b["after"]["var_1-day"],
        "book_es_1d_before": b["before"]["es_1-day"], "book_es_1d_after": b["after"]["es_1-day"],
        "book_var_10d_before": b["before"]["var_10-day"], "book_var_10d_after": b["after"]["var_10-day"],
        "book_es_10d_before": b["before"]["es_10-day"], "book_es_10d_after": b["after"]["es_10-day"],
        "firm_var_1d_before": f["before"]["var_1-day"], "firm_var_1d_after": f["after"]["var_1-day"],
        "firm_es_1d_before": f["before"]["es_1-day"], "firm_es_1d_after": f["after"]["es_1-day"],
        "cpty_netting_key": cp["netting_key"],
        "cpty_peak_pfe_before": cp["before"]["peak_pfe"], "cpty_peak_pfe_after": cp["after"]["peak_pfe"],
        "cpty_peak_ee_before": cp["before"]["peak_ee"], "cpty_peak_ee_after": cp["after"]["peak_ee"],
        "cpty_epe_1y_before": cp["before"]["epe_1y"], "cpty_epe_1y_after": cp["after"]["epe_1y"],
        "trade_peak_pfe": cp["trade"]["peak_pfe"], "compute_seconds": compute_seconds,
    }


def write(r: dict, wait: str = "20s") -> tuple[str, str]:
    """Insert one audit row. Returns (status, detail): status is 'written', 'pending' (still running on the
    warehouse, it will complete) or 'failed'."""
    if not WAREHOUSE:
        return "failed", "no SQL warehouse configured (WHATIF_AUDIT_WAREHOUSE)"
    audit_id = str(uuid.uuid4())
    params = [StatementParameterListItem(name="audit_id", value=audit_id, type="STRING")]
    for c in COLUMNS:
        v = _clean(r.get(c))
        params.append(StatementParameterListItem(name=c, value=None if v is None else str(v)))
    try:
        resp = _w.statement_execution.execute_statement(warehouse_id=WAREHOUSE, statement=INSERT,
                                                        parameters=params, wait_timeout=wait)
    except Exception as e:
        return "failed", str(e).splitlines()[0][:300]
    state = resp.status.state if resp.status else None
    if state == StatementState.SUCCEEDED:
        return "written", audit_id
    if state in (StatementState.PENDING, StatementState.RUNNING):
        return "pending", audit_id
    msg = resp.status.error.message if resp.status and resp.status.error else str(state)
    return "failed", (msg or "").splitlines()[0][:300]
