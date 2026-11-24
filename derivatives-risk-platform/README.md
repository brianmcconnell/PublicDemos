On A clean workspace:

Clone the repo, then run everything from the derivatives-risk-platform subfolder.

Before you start, make sure that workspace has:
- The target catalog;
- A SQL warehouse ID;
- The FRED API key stored as a UC secret;
- Lakebase enabled;
- A CLI profile (CLI 1.0 or later) plus databricks-sdk and psycopg[binary] installed locally.

Run databricks bundle validate -t fresh --var catalog=<catalog>, then riskctl.py status, which should report everything absent, then riskctl.py deploy.

For the bundle deploy  you will need parameters for your catalog, schema, warehouse, a secret for your personal API key for data loading from https://fred.stlouisfed.org/ (St Louis Federal Reserve). Obviously, not the values below - you need to set your own and you can choose to have the job PAUSED or not

{
  "catalog": "catalog_name",
  "schema": "schema_name",
  "app_warehouse_id": "371cda125ac45a67",
  "fred_secret": "risk_test_bm_catalog.risk_app.fred_api_key",
  "daily_schedule_status": "PAUSED"
}
