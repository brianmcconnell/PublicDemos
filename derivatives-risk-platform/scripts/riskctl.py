#!/usr/bin/env python3
"""riskctl: build, inspect and clean down a fresh derivatives-risk-platform environment.

Run from your machine (needs the Databricks CLI >= 1.0 and `pip install databricks-sdk psycopg[binary]`), with a
CLI profile for the target workspace. Everything is keyed on the catalog, so one workspace can hold several
environments as long as the app name and Lakebase project differ.

  python scripts/riskctl.py export  -p SOURCE --catalog <source_catalog>       # once: dashboard + Genie into src/
  python scripts/riskctl.py status  -p TARGET --catalog <catalog>              # pre-flight report only
  python scripts/riskctl.py deploy  -p TARGET --catalog <catalog> --warehouse-id <id> --fred-secret <cat.schema.key>
  python scripts/riskctl.py destroy -p TARGET --catalog <catalog> --confirm <catalog> [--purge-lakebase]

deploy halts (exit 2) and reports if any part of an existing version is found; use destroy first, or --resume to
continue a deploy that stopped part-way. Steps: Lakebase project -> schema + sim_cubes volume -> bundle deploy
(jobs + app) -> bootstrap job (setup -> backfill -> first daily run -> Lakebase synced tables, riskapp_cfg, audit
table, grants) -> dashboard + Genie agent -> app code deploy -> unpause the daily schedule.
"""
import argparse, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from lakebase_spec import SYNC_SCHEMA, SYNCED_TABLES, synced_name   # noqa: E402

TARGET = "fresh"
BUNDLE = "derivatives-risk-platform"
JOB_PREFIX = f"[{TARGET}] risk-platform-"
DASH_FILE = os.path.join(ROOT, "src", "dashboards", "derivatives_risk.lvdash.json")
GENIE_FILE = os.path.join(ROOT, "src", "genie", "derivatives_risk_var_attribution.json")
DASH_NAME = "Derivatives Risk Dashboard"
GENIE_TITLE = "Derivatives Risk VaR Attribution"
CAT, SCH = "__CATALOG__", "__SCHEMA__"          # placeholders in the exported definitions


def client(profile):
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient(profile=profile)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def paths(w, a):
    me = w.current_user.me().user_name
    return dict(me=me,
                bundle_root=f"/Workspace/Users/{me}/.bundle/{BUNDLE}/{TARGET}/{a.catalog}",
                assets=f"/Workspace/Users/{me}/{BUNDLE}-assets/{a.catalog}")


def sql(w, warehouse_id, stmt):
    from databricks.sdk.service.sql import StatementState
    r = w.statement_execution.execute_statement(statement=stmt, warehouse_id=warehouse_id, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(3)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"{stmt[:80]}: {r.status.state} {r.status.error.message if r.status.error else ''}")
    return r.result.data_array if r.result else []


def bundle(a, *args):
    cmd = ["databricks", "bundle", *args, "-t", TARGET, "-p", a.profile,
           f"--var=catalog={a.catalog}", f"--var=schema={a.schema}", f"--var=app_name={a.app_name}",
           f"--var=lakebase_project={a.lakebase_project}"]
    for k in ("fred_secret", "warehouse_id"):
        if getattr(a, k, None):
            cmd.append(f"--var={'app_warehouse_id' if k == 'warehouse_id' else k}={getattr(a, k)}")
    cmd += getattr(a, "_extra_vars", [])
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def exists(fn):
    try:
        return fn()
    except Exception as e:
        if type(e).__name__ in ("NotFound", "ResourceDoesNotExist") or "does not exist" in str(e).lower() \
                or "not found" in str(e).lower():
            return None
        raise


# ---- pre-flight ---------------------------------------------------------------------------------------------
def preflight(w, a):
    """[(component, identifier, found: bool, detail)] for every asset a deploy creates."""
    p, rows = paths(w, a), []
    add = lambda comp, ident, obj, detail="": rows.append((comp, ident, obj is not None and obj is not False, detail))
    add("UC schema", f"{a.catalog}.{a.schema}", exists(lambda: w.schemas.get(f"{a.catalog}.{a.schema}")))
    add("UC schema (synced tables)", f"{a.catalog}.{SYNC_SCHEMA}", exists(lambda: w.schemas.get(f"{a.catalog}.{SYNC_SCHEMA}")))
    add("Lakebase project", f"projects/{a.lakebase_project}", exists(lambda: w.postgres.get_project(f"projects/{a.lakebase_project}")))
    st = [t for t in SYNCED_TABLES if exists(lambda t=t: w.postgres.get_synced_table(synced_name(a.catalog, t)))]
    add("Lakebase synced tables", f"{a.catalog}.{SYNC_SCHEMA}.*", st or None, f"{len(st)} of {len(SYNCED_TABLES)}")
    app = exists(lambda: w.apps.get(a.app_name))
    state = getattr(getattr(app, "compute_status", None), "state", None) if app else None
    add("Databricks App", a.app_name, app, getattr(state, "value", str(state)) if state else "")
    jobs = [j.settings.name for j in w.jobs.list(name=None) if j.settings and (j.settings.name or "").startswith(JOB_PREFIX)]
    add("Jobs", JOB_PREFIX + "*", jobs or None, ", ".join(jobs))
    add("Bundle deployment state", p["bundle_root"], exists(lambda: w.workspace.get_status(p["bundle_root"])))
    add("Dashboard / Genie folder", p["assets"], exists(lambda: w.workspace.get_status(p["assets"])))
    return rows


def report(rows, title):
    print(f"\n{title}\n" + "-" * len(title))
    for comp, ident, found, detail in rows:
        print(f"  {'EXISTS ' if found else 'absent '} {comp:<28} {ident}{'  (' + str(detail) + ')' if detail else ''}")
    print()
    return any(r[2] for r in rows)


def cmd_status(a):
    w = client(a.profile)
    if not exists(lambda: w.catalogs.get(a.catalog)):
        sys.exit(f"Catalog {a.catalog} not found in {w.config.host} - create it (or pick another) first.")
    found = report(preflight(w, a), f"Environment check: catalog {a.catalog} on {w.config.host}")
    print("Existing version found." if found else "Clean - nothing deployed for this catalog.")
    return 2 if found else 0


# ---- export (source workspace, once) -----------------------------------------------------------------------
def cmd_export(a):
    w = client(a.profile)
    src = f"{a.catalog}.{a.schema}"
    d = w.lakeview.get(a.dashboard_id).serialized_dashboard
    g = w.genie.get_space(a.genie_id, include_serialized_space=True)
    for f, body in ((DASH_FILE, d), (GENIE_FILE, g.serialized_space)):
        n = body.count(src)
        body = body.replace(src, f"{CAT}.{SCH}")
        if a.catalog in body:
            sys.exit(f"{f}: '{a.catalog}' still referenced outside {src} - fix the source asset first")
        os.makedirs(os.path.dirname(f), exist_ok=True)
        if f == GENIE_FILE:
            body = json.dumps({"title": g.title, "description": g.description, "serialized_space": body}, indent=1)
        open(f, "w").write(body)
        log(f"wrote {os.path.relpath(f, ROOT)} ({n} table references -> {CAT}.{SCH})")


# ---- deploy -------------------------------------------------------------------------------------------------
def cmd_deploy(a):
    from databricks.sdk.service import postgres as pg
    from databricks.sdk.service.dashboards import Dashboard
    w = client(a.profile)
    for f in (DASH_FILE, GENIE_FILE):
        if not os.path.exists(f):
            sys.exit(f"{os.path.relpath(f, ROOT)} missing - run `riskctl.py export` against the source workspace first")
    if not exists(lambda: w.catalogs.get(a.catalog)):
        sys.exit(f"Catalog {a.catalog} not found in {w.config.host} - create it first.")
    w.warehouses.get(a.warehouse_id)
    rows = preflight(w, a)
    if report(rows, f"Pre-flight: catalog {a.catalog} on {w.config.host}") and not a.resume:
        print("HALTED: an existing version was found (EXISTS above). Nothing was changed.\n"
              f"  Clean it down:  python scripts/riskctl.py destroy -p {a.profile} --catalog {a.catalog} --confirm {a.catalog}\n"
              "  or continue a deploy that stopped part-way with --resume.")
        return 2
    p = paths(w, a)

    log("1/8 Lakebase project")
    proj = f"projects/{a.lakebase_project}"
    if not exists(lambda: w.postgres.get_project(proj)):
        w.postgres.create_project(pg.Project(spec=pg.ProjectSpec(
            display_name=a.lakebase_project, pg_version=a.pg_version,
            default_endpoint_settings=pg.ProjectDefaultEndpointSettings(autoscaling_limit_min_cu=a.min_cu,
                                                                        autoscaling_limit_max_cu=a.max_cu))),
            project_id=a.lakebase_project).wait()
    log(f"    {proj} ready")

    log("2/8 UC schema + sim_cubes volume (the app's volume resource must exist before the app is created)")
    sql(w, a.warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {a.catalog}.{a.schema}")
    sql(w, a.warehouse_id, f"CREATE VOLUME IF NOT EXISTS {a.catalog}.{a.schema}.sim_cubes")

    log("3/8 bundle deploy (jobs, app; daily schedule PAUSED)")
    bundle(a, "deploy")

    if not a.skip_data:
        log("4/8 bootstrap job: setup -> backfill -> first daily run -> configure_serving (several hours)")
        bundle(a, "run", "bootstrap")

    log("5/8 dashboard")
    w.workspace.mkdirs(p["assets"])
    dash = open(DASH_FILE).read().replace(CAT, a.catalog).replace(SCH, a.schema)
    d = w.lakeview.create(Dashboard(display_name=DASH_NAME, parent_path=p["assets"], serialized_dashboard=dash,
                                    warehouse_id=a.warehouse_id))
    w.lakeview.publish(d.dashboard_id, warehouse_id=a.warehouse_id, embed_credentials=True)

    log("6/8 Genie agent")
    gd = json.load(open(GENIE_FILE))
    g = w.genie.create_space(a.warehouse_id, gd["serialized_space"].replace(CAT, a.catalog).replace(SCH, a.schema),
                             title=gd.get("title") or GENIE_TITLE, description=gd.get("description"),
                             parent_path=p["assets"])
    state = dict(catalog=a.catalog, schema=a.schema, dashboard_id=d.dashboard_id, genie_space_id=g.space_id,
                 app_name=a.app_name, lakebase_project=a.lakebase_project, deployed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    import io
    from databricks.sdk.service.workspace import ImportFormat
    w.workspace.upload(f"{p['assets']}/deploy_state.json", io.BytesIO(json.dumps(state, indent=1).encode()),
                       format=ImportFormat.AUTO, overwrite=True)

    log("7/8 app code deploy (bundle run risk_curves_app)")
    bundle(a, "run", "risk_curves_app")

    if not a.skip_data:
        log("8/8 unpause the daily schedule")
        a._extra_vars = ["--var=daily_schedule_status=UNPAUSED"]
        bundle(a, "deploy")
    app = w.apps.get(a.app_name)
    report(preflight(w, a), "Deployed")
    print(f"App:       {app.url}\nDashboard: {w.config.host}/dashboardsv3/{d.dashboard_id}/published\n"
          f"Genie:     {w.config.host}/genie/rooms/{g.space_id}\n")
    return 0


# ---- destroy ------------------------------------------------------------------------------------------------
def cmd_destroy(a):
    w = client(a.profile)
    if a.confirm != a.catalog:
        sys.exit(f"Refusing: pass --confirm {a.catalog} to delete every asset of this environment.")
    p = paths(w, a)
    if not exists(lambda: w.workspace.get_status(p["bundle_root"])) and not a.force:
        sys.exit(f"Refusing: {p['bundle_root']} not found, so this catalog was not deployed by riskctl (fresh target). "
                 "Use --force only if you are sure these assets belong to a riskctl environment.")
    report(preflight(w, a), f"Removing: catalog {a.catalog} on {w.config.host}")
    errors = []

    def step(name, fn):
        log(name)
        try:
            fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
            log(f"    FAILED: {e}")

    def assets():
        if not exists(lambda: w.workspace.get_status(p["assets"])):
            return
        try:
            s = json.loads(w.workspace.download(f"{p['assets']}/deploy_state.json").read())
        except Exception:
            s = {}
        if s.get("genie_space_id"):
            exists(lambda: w.genie.trash_space(s["genie_space_id"]))
        if s.get("dashboard_id"):
            exists(lambda: w.lakeview.trash(s["dashboard_id"]))
        w.workspace.delete(p["assets"], recursive=True)

    def synced():
        for t in SYNCED_TABLES:
            if exists(lambda t=t: w.postgres.get_synced_table(synced_name(a.catalog, t))):
                w.postgres.delete_synced_table(synced_name(a.catalog, t)).wait()
                log(f"    synced table {t} deleted")

    def lakebase():
        if exists(lambda: w.postgres.get_project(f"projects/{a.lakebase_project}")):
            w.postgres.delete_project(f"projects/{a.lakebase_project}", purge=a.purge_lakebase).wait()

    def schemas():
        wh = a.warehouse_id or next(x.id for x in w.warehouses.list())
        for s in (SYNC_SCHEMA, a.schema):        # CASCADE drops tables, volumes (sim cubes) and the mask function
            sql(w, wh, f"DROP SCHEMA IF EXISTS {a.catalog}.{s} CASCADE")

    step("1/5 dashboard, Genie agent and their folder", assets)
    step("2/5 bundle destroy (jobs, app)", lambda: bundle(a, "destroy", "--auto-approve"))
    step("3/5 Lakebase synced tables", synced)
    step(f"4/5 Lakebase project ({'purged' if a.purge_lakebase else 'soft-deleted, recoverable for 7 days'})", lakebase)
    step(f"5/5 UC schemas {a.catalog}.{SYNC_SCHEMA} and {a.catalog}.{a.schema} (CASCADE)", schemas)
    left = report(preflight(w, a), "After clean-down")
    if errors or left:
        print("Clean-down incomplete:\n  " + "\n  ".join(errors or ["see EXISTS rows above"]))
        return 1
    print("Clean - every asset of this environment has been removed.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "deploy", "destroy", "export"):
        s = sub.add_parser(name)
        s.add_argument("-p", "--profile", required=True, help="Databricks CLI profile of the workspace")
        s.add_argument("--catalog", required=True)
        s.add_argument("--schema", default="market")
        s.add_argument("--app-name", dest="app_name", default="risk-curves-app")
        s.add_argument("--lakebase-project", dest="lakebase_project", default="riskapp")
        s.add_argument("--warehouse-id", dest="warehouse_id", required=(name == "deploy"))
    d = sub.choices["deploy"]
    d.add_argument("--fred-secret", dest="fred_secret", required=True, help="UC secret <catalog>.<schema>.<key> holding the FRED API key")
    d.add_argument("--pg-version", dest="pg_version", type=int, default=17)
    d.add_argument("--min-cu", dest="min_cu", type=float, default=0.5)
    d.add_argument("--max-cu", dest="max_cu", type=float, default=4.0)
    d.add_argument("--resume", action="store_true", help="continue a deploy that stopped part-way (skips the halt)")
    d.add_argument("--skip-data", dest="skip_data", action="store_true", help="deploy assets only, no bootstrap run")
    x = sub.choices["destroy"]
    x.add_argument("--confirm", required=True, help="must equal --catalog")
    x.add_argument("--purge-lakebase", dest="purge_lakebase", action="store_true")
    x.add_argument("--force", action="store_true")
    e = sub.choices["export"]
    e.add_argument("--dashboard-id", dest="dashboard_id", default="01f1cdecf19b17968c2a64df6e46cc64")
    e.add_argument("--genie-id", dest="genie_id", default="01f1d6ece8cf1c619fd560659eca8b38")
    a = ap.parse_args()
    sys.exit({"status": cmd_status, "deploy": cmd_deploy, "destroy": cmd_destroy, "export": cmd_export}[a.cmd](a) or 0)


if __name__ == "__main__":
    main()
