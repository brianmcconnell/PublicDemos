"""wh.py: thin SQL warehouse query helper for the Risk Curves app.

Used by forecast_tab.py to query gold_book_var and gold_var_forecast which live in the
lakehouse and are not synced to Lakebase.  Uses the Databricks SDK's SQL statement
execution API (token-authenticated via the app's service principal context).
"""
from __future__ import annotations
import os, time, pandas as pd
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, StatementState

# Warehouse ID injected via app.yaml resource / environment variable
_WH_ID = os.environ.get("FORECAST_WH_ID", os.environ.get("DATABRICKS_WAREHOUSE_ID", ""))
_w     = WorkspaceClient()


def wh_query(sql: str, timeout_seconds: int = 120) -> pd.DataFrame:
    """Run a SQL statement on the serverless warehouse and return a pandas DataFrame."""
    if not _WH_ID:
        raise RuntimeError("FORECAST_WH_ID is not set")
    # The SDK requires enum values here; a plain "INLINE" string fails with
    # "'str' object has no attribute 'value'".
    resp = _w.statement_execution.execute_statement(
        warehouse_id=_WH_ID,
        statement=sql,
        wait_timeout="30s",
        disposition=Disposition.INLINE,
    )
    # A cold warehouse can take longer than wait_timeout: poll until the statement finishes.
    deadline = time.time() + timeout_seconds
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.time() > deadline:
            _w.statement_execution.cancel_execution(resp.statement_id)
            raise RuntimeError(f"SQL timed out after {timeout_seconds}s")
        time.sleep(2)
        resp = _w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed ({resp.status.state}): {resp.status.error}")

    cols = [c.name for c in resp.manifest.schema.columns]
    rows = list((resp.result.data_array if resp.result else None) or [])
    # Collect any additional result chunks
    nxt = resp.result.next_chunk_index if resp.result else None
    while nxt is not None:
        chunk = _w.statement_execution.get_statement_result_chunk_n(resp.statement_id, nxt)
        rows.extend(chunk.data_array or [])
        nxt = chunk.next_chunk_index
    return pd.DataFrame(rows, columns=cols)
