"""wh.py: thin SQL warehouse query helper for the Risk Curves app.

Used by forecast_tab.py to query gold_book_var and gold_var_forecast which live in the
lakehouse and are not synced to Lakebase.  Uses the Databricks SDK's SQL statement
execution API (token-authenticated via the app's service principal context).
"""
from __future__ import annotations
import os, pandas as pd
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

# Warehouse ID injected via app.yaml resource / environment variable
_WH_ID = os.environ.get("FORECAST_WH_ID", os.environ.get("DATABRICKS_WAREHOUSE_ID", ""))
_w     = WorkspaceClient()


def wh_query(sql: str, timeout_seconds: int = 30) -> pd.DataFrame:
    """Run a SQL statement on the serverless warehouse and return a pandas DataFrame."""
    resp = _w.statement_execution.execute_statement(
        warehouse_id=_WH_ID,
        statement=sql,
        wait_timeout=f"{timeout_seconds}s",
        disposition="INLINE",
    )
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed ({resp.status.state}): {resp.status.error}")
    result = resp.result
    if not result or not result.data_array:
        return pd.DataFrame()
    cols = [c.name for c in resp.manifest.schema.columns]
    return pd.DataFrame(result.data_array, columns=cols)
