"""configure_serving: wire the loaded lakehouse data to Lakebase riskapp and the app (bootstrap job, last task).

Idempotent - safe to rerun. Runs after setup -> backfill -> first daily run, when the source tables exist.
  1. UC: schema <catalog>.riskapp (synced-table home), what-if audit table + run_by mask + app grants
  2. Lakebase synced tables <catalog>.riskapp.* (TRIGGERED, one shared pipeline), waits until online
  3. Postgres: riskapp_cfg schema (tenor_config + guard/audit triggers + view), seeded from ref_tenor
  4. Postgres grants for the app service principal (role created when the app's postgres resource was attached)
"""
import argparse, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pyspark.sql import SparkSession
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service import postgres as pg
from lakebase_spec import (PG_DATABASE, SYNC_SCHEMA, CFG_SCHEMA, SYNCED_TABLES, branch, endpoint, synced_name)

ap = argparse.ArgumentParser()
ap.add_argument("--catalog", required=True)
ap.add_argument("--schema", default="market")
ap.add_argument("--lakebase_project", default="riskapp")
ap.add_argument("--app_name", required=True)
ap.add_argument("--online_timeout_minutes", type=int, default=60)
a, _ = ap.parse_known_args()

spark = SparkSession.builder.getOrCreate()
w = WorkspaceClient()
SQL_DIR = os.path.join(HERE, "..", "sql")
problems = []

app = w.apps.get(a.app_name)
APP_SP = app.service_principal_client_id
print(f"app {a.app_name}: service principal {APP_SP}")

# 1. Unity Catalog ------------------------------------------------------------------------------------------
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {a.catalog}.{SYNC_SCHEMA} COMMENT 'Lakebase riskapp synced tables'")
ddl = open(os.path.join(SQL_DIR, "whatif_run_audit.sql")).read()
ddl = ddl.replace("{{CATALOG}}", a.catalog).replace("{{SCHEMA}}", a.schema).replace("{{APP_SP}}", APP_SP)
ddl = "\n".join(l for l in ddl.splitlines() if not l.strip().startswith("--"))   # comments may contain ';'
for stmt in ddl.split(";"):
    body = stmt.strip()
    if body:
        spark.sql(body)
        print("UC:", body.splitlines()[0][:110])

# 2. Synced tables ------------------------------------------------------------------------------------------
for src, _ in SYNCED_TABLES.values():           # TRIGGERED sync needs Change Data Feed (06 sets it; make sure)
    spark.sql(f"ALTER TABLE {a.catalog}.{a.schema}.{src} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")


def synced(t):
    try:
        return w.postgres.get_synced_table(synced_name(a.catalog, t))
    except NotFound:
        return None


pipeline_id = next((s.status.pipeline_id for s in map(synced, SYNCED_TABLES) if s and s.status and s.status.pipeline_id), None)
for t, (src, pk) in SYNCED_TABLES.items():
    if synced(t):
        print(f"synced table {t}: exists")
        continue
    spec = pg.SyncedTableSyncedTableSpec(
        branch=branch(a.lakebase_project), postgres_database=PG_DATABASE,
        source_table_full_name=f"{a.catalog}.{a.schema}.{src}", primary_key_columns=pk,
        scheduling_policy=pg.SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy.TRIGGERED,
        create_database_objects_if_missing=True,
        **({"existing_pipeline_id": pipeline_id} if pipeline_id else
           {"new_pipeline_spec": pg.NewPipelineSpec(storage_catalog=a.catalog, storage_schema=SYNC_SCHEMA)}))
    tid = f"{a.catalog}.{SYNC_SCHEMA}.{t}"
    w.postgres.create_synced_table(pg.SyncedTable(spec=spec), synced_table_id=tid).wait()
    print(f"synced table {t}: created from {src} (pk {pk})")
    if not pipeline_id:                          # every later table is bin-packed into the first one's pipeline
        for _ in range(60):
            s = synced(t)
            if s and s.status and s.status.pipeline_id:
                pipeline_id = s.status.pipeline_id
                break
            time.sleep(10)
        print(f"  shared sync pipeline {pipeline_id}")

deadline = time.time() + a.online_timeout_minutes * 60
pending = set(SYNCED_TABLES)
while pending and time.time() < deadline:
    for t in sorted(pending):
        s = synced(t)
        state = s.status.detailed_state.value if s and s.status and s.status.detailed_state else "UNKNOWN"
        if state.startswith("SYNCED_TABLE_ONLINE"):
            pending.discard(t)
        elif "FAILED" in state:
            problems.append(f"synced table {t}: {state} {s.status.message}")
            pending.discard(t)
    if pending:
        time.sleep(30)
if pending:
    problems.append(f"synced tables not online after {a.online_timeout_minutes} min: {sorted(pending)}")
print("synced tables online:", len(SYNCED_TABLES) - len(pending) - sum('synced table' in p for p in problems))

# 3./4. Postgres: riskapp_cfg + grants ---------------------------------------------------------------------
import psycopg
ep = w.postgres.get_endpoint(endpoint(a.lakebase_project))
cred = w.postgres.generate_database_credential(endpoint=endpoint(a.lakebase_project))
me = w.current_user.me().user_name
tenors = spark.sql(f"SELECT tenor_label, tenor_years, on_curve_grid FROM {a.catalog}.{a.schema}.ref_tenor").collect()
with psycopg.connect(host=ep.status.hosts.host, dbname=PG_DATABASE, user=me, password=cred.token,
                     sslmode="require", autocommit=True) as conn:
    conn.execute(open(os.path.join(SQL_DIR, "riskapp_cfg.pg.sql")).read())
    print("Postgres: riskapp_cfg DDL applied")
    with conn.cursor() as cur:
        cur.executemany(f"INSERT INTO {CFG_SCHEMA}.tenor_config (tenor_label, tenor_years, on_curve_grid) "
                        "VALUES (%s, %s, %s) ON CONFLICT (tenor_label) DO NOTHING",
                        [(r.tenor_label, float(r.tenor_years), bool(r.on_curve_grid)) for r in tenors])
    n = conn.execute(f"SELECT count(*) FROM {CFG_SCHEMA}.tenor_config").fetchone()[0]
    print(f"Postgres: tenor_config has {n} rows (seeded from {len(tenors)} ref_tenor rows)")
    sp = f'"{APP_SP}"'
    for g in [f"GRANT USAGE ON SCHEMA {SYNC_SCHEMA}, {CFG_SCHEMA} TO {sp}",
              f"GRANT SELECT ON ALL TABLES IN SCHEMA {SYNC_SCHEMA} TO {sp}",
              f"GRANT SELECT, INSERT, UPDATE ON {CFG_SCHEMA}.tenor_config TO {sp}",
              f"GRANT SELECT ON {CFG_SCHEMA}.tenor_config_audit, {CFG_SCHEMA}.active_tenors TO {sp}"]:
        try:
            conn.execute(g)
            print("Postgres:", g)
        except Exception as e:
            problems.append(f"{g}: {e}")

if problems:
    print("\nPROBLEMS:\n  " + "\n  ".join(problems))
    raise RuntimeError(f"configure_serving finished with {len(problems)} problem(s)")
print("\nconfigure_serving complete")
