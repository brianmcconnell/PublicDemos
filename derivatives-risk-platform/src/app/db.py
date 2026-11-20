"""Lakebase (Postgres) access for risk-curves-app.

Connects as the app's service principal using an OAuth token minted for the Lakebase endpoint.
Tokens last about an hour, so the shared connection is replaced every 45 minutes or when it drops.
Connection settings come from the env vars injected by the app's `postgres` resource, with fallbacks to the
riskapp project so the module also works from a notebook.
"""
from __future__ import annotations

import os
import threading
import time

import pandas as pd
import psycopg
from databricks.sdk import WorkspaceClient

ENDPOINT = os.environ.get("LAKEBASE_ENDPOINT", "projects/riskapp/branches/production/endpoints/primary")
HOST = os.environ.get("PGHOST", "ep-withered-butterfly-d2m5bl4s.database.us-east-1.cloud.databricks.com")
DBNAME = os.environ.get("PGDATABASE", "databricks_postgres")
PORT = int(os.environ.get("PGPORT", "5432"))
TOKEN_TTL_S = 45 * 60

_w = WorkspaceClient()
_lock = threading.Lock()
_conn: psycopg.Connection | None = None
_born = 0.0


def _user() -> str:
    return os.environ.get("PGUSER") or _w.current_user.me().user_name


def _connection() -> psycopg.Connection:
    global _conn, _born
    if _conn is None or _conn.closed or time.time() - _born > TOKEN_TTL_S:
        if _conn is not None and not _conn.closed:
            _conn.close()
        token = _w.postgres.generate_database_credential(endpoint=ENDPOINT).token
        _conn = psycopg.connect(host=HOST, port=PORT, dbname=DBNAME, user=_user(), password=token,
                                sslmode="require", autocommit=True, application_name="risk-curves-app")
        _born = time.time()
    return _conn


def query(sql: str, params: tuple | dict | None = None) -> pd.DataFrame:
    """Run a read query and return a DataFrame. Retries once on a dropped connection."""
    global _conn
    for attempt in (1, 2):
        with _lock:
            try:
                cur = _connection().execute(sql, params)
                cols = [d.name for d in cur.description]
                return pd.DataFrame(cur.fetchall(), columns=cols)
            except psycopg.OperationalError:
                _conn = None
                if attempt == 2:
                    raise


def execute(sql: str, params: tuple | dict | None = None) -> int:
    """Run one write (or DDL batch without params) in a transaction. Returns rowcount."""
    global _conn
    for attempt in (1, 2):
        with _lock:
            try:
                conn = _connection()
                with conn.transaction():
                    return conn.execute(sql, params).rowcount
            except psycopg.OperationalError:
                _conn = None
                if attempt == 2:
                    raise


def execute_as(user_email: str, sql: str, params: tuple | dict | None = None) -> int:
    """Run one write in a transaction, recording `user_email` in the tenor audit trail. Returns rowcount."""
    global _conn
    for attempt in (1, 2):
        with _lock:
            try:
                conn = _connection()
                with conn.transaction():
                    conn.execute("SELECT set_config('riskapp.user', %s, true)", (user_email,))
                    return conn.execute(sql, params).rowcount
            except psycopg.OperationalError:
                _conn = None
                if attempt == 2:
                    raise
