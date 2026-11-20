"""Lakebase (riskapp) layout shared by configure_serving.py, refresh_lakebase_sync.py and scripts/riskctl.py.

Synced tables live in UC as <catalog>.riskapp.<name> and in Postgres as databricks_postgres.riskapp.<name>.
Primary keys match the production riskapp project (read from its information_schema on 2026-11-17).
"""
PG_DATABASE = "databricks_postgres"
SYNC_SCHEMA = "riskapp"           # UC schema holding the synced tables == Postgres schema they land in
CFG_SCHEMA = "riskapp_cfg"        # writable app config (tenor_config + audit), see src/sql/riskapp_cfg.pg.sql
WHATIF_SCHEMA = "riskapp_whatif"  # created and owned by the app service principal on first page load

# synced table name -> (source table in <catalog>.<schema>, primary key columns)
SYNCED_TABLES = {
    "ref_currency":         ("ref_currency",         ["ccy"]),
    "ref_tenor":            ("ref_tenor",            ["tenor_label"]),
    "ref_rate":             ("ref_rate",             ["rate_id"]),
    "ref_curve_date":       ("ref_curve_date",       ["ccy", "as_of_date"]),
    "ref_sim_step":         ("ref_sim_step",         ["sim_as_of_date", "time_step"]),
    "ref_equity":           ("ref_equity",           ["ticker"]),
    "ref_sim_curve":        ("ref_sim_curve",        ["sim_as_of_date", "ccy", "tenor_label"]),
    "sim_rate_state":       ("sim_rate_state",       ["sim_as_of_date", "ccy", "time_step", "path_id"]),
    "zero_curve_history":   ("gold_zero_curves",     ["as_of_date", "ccy", "tenor_label"]),
    "hw_params_history":    ("gold_hw_params",       ["as_of_date", "ccy"]),
    "fx_rate_history":      ("bronze_fx_rates",      ["ccy", "obs_date"]),
    "equity_price_history": ("bronze_equity_prices", ["ticker", "trade_date"]),
}


def branch(project: str) -> str:
    return f"projects/{project}/branches/production"


def endpoint(project: str) -> str:
    return f"{branch(project)}/endpoints/primary"


def synced_name(catalog: str, table: str) -> str:
    return f"synced_tables/{catalog}.{SYNC_SCHEMA}.{table}"
