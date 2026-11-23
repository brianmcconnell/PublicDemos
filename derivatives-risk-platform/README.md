On A clean workspace:

Clone the repo, then run everything from the derivatives-risk-platform subfolder.

Before you start, make sure that workspace has:
- The target catalog;
- A SQL warehouse ID;
- The FRED API key stored as a UC secret;
- Lakebase enabled;
- A CLI profile (CLI 1.0 or later) plus databricks-sdk and psycopg[binary] installed locally.

Run databricks bundle validate -t fresh --var catalog=<catalog>, then riskctl.py status, which should report everything absent, then riskctl.py deploy.
