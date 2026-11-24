"""Trigger a sync of every <catalog>.riskapp synced table (risk_daily task refresh_lakebase_sync).

The synced tables share one pipeline per environment; it is found from the synced tables themselves, so no
pipeline id is configured. If no synced table exists yet (first bootstrap run, before configure_serving) it skips.
"""
import argparse, inspect, os, sys, time
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:  # serverless exec() context — __file__ not set
    sys.path.insert(0, os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe()))))
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from lakebase_spec import SYNCED_TABLES, synced_name

ap = argparse.ArgumentParser()
ap.add_argument("--catalog", required=True)
ap.add_argument("--wait_minutes", type=int, default=45)
a, _ = ap.parse_known_args()
w = WorkspaceClient()

pipelines = set()
for t in SYNCED_TABLES:
    try:
        st = w.postgres.get_synced_table(synced_name(a.catalog, t))
        if st.status and st.status.pipeline_id:
            pipelines.add(st.status.pipeline_id)
    except NotFound:
        print(f"{t}: synced table not created yet")
if not pipelines:
    print("No synced tables yet - nothing to refresh (configure_serving creates them).")
    sys.exit(0)

updates = {}
for pid in sorted(pipelines):
    try:
        updates[pid] = w.pipelines.start_update(pid, full_refresh=False).update_id
        print(f"pipeline {pid}: update {updates[pid]} started")
    except Exception as e:                       # an update already running counts as a refresh
        print(f"pipeline {pid}: not started ({e})")

deadline = time.time() + a.wait_minutes * 60
failed = []
for pid, uid in updates.items():
    while True:
        state = w.pipelines.get_update(pid, uid).update.state.value
        if state in ("COMPLETED", "FAILED", "CANCELED"):
            print(f"pipeline {pid}: {state}")
            if state != "COMPLETED":
                failed.append(pid)
            break
        if time.time() > deadline:
            raise TimeoutError(f"pipeline {pid} update {uid} still {state} after {a.wait_minutes} min")
        time.sleep(20)
if failed:
    raise RuntimeError(f"Lakebase sync failed for pipelines {failed}")
