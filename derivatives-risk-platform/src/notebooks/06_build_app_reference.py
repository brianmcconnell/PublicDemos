"""06_build_app_reference: reference / picker tables for the risk-curves-app.

Builds small Delta tables (Change Data Feed on) in the market schema that are synced to the
Lakebase project `riskapp` (database databricks_postgres, schema riskapp) as synced tables:
  ref_currency     one row per curve currency (date range, HW params of latest calibration)
  ref_tenor        curve grid tenors + off-grid tenors read off the reconstructed curve
  ref_rate         every pickable simulated rate: SHORT (HW1F state) and zero rates per tenor
  ref_curve_date   (ccy, as_of_date) pairs with a historical curve
  ref_sim_step     simulation grid of the latest cube (time_step -> sim_date, t_years)
  ref_equity       equity tickers and currency
  ref_sim_curve    initial zero curve each currency was simulated from (latest cube)
  sim_rate_state   HW1F state x(t) per currency, time step and path (latest cube); the app rebuilds
                   any tenor from it: P(t,t+tau|x) = A(t,t+tau) exp(-B(tau) x)
Re-run after 02_bootstrap_curves / 05_mc_simulation so the synced tables pick up new values.
"""
import os, sys, json, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))
import numpy as np
import pandas as pd
from pyspark.sql import functions as F

from riskcore.config import TENORS, OFF_GRID_TENORS, SHORT_RATE_LABEL

def _widget(name, default):
    try:
        dbutils.widgets.text(name, default)
        return dbutils.widgets.get(name) or default
    except Exception:
        return default


CATALOG = _widget("catalog", "serverless_stable_7s0j2u_catalog")
SCHEMA = _widget("schema", "market")
T = lambda n: f"{CATALOG}.{SCHEMA}.{n}"
CUBE_DIR = f"/Volumes/{CATALOG}/{SCHEMA}/sim_cubes"

CCY_NAMES = {"USD": "US Dollar", "EUR": "Euro", "GBP": "Pound Sterling", "JPY": "Japanese Yen",
             "CHF": "Swiss Franc", "CAD": "Canadian Dollar"}
GRID = TENORS                 # bootstrap grid (riskcore.config)
OFF_GRID = OFF_GRID_TENORS    # read off the curve rebuilt from the simulated HW1F state


def write(pdf: pd.DataFrame, name: str):
    (spark.createDataFrame(pdf).write.mode("overwrite").option("overwriteSchema", "true")
          .saveAsTable(T(name)))
    spark.sql(f"ALTER TABLE {T(name)} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
    print(f"{name}: {len(pdf)} rows")

# COMMAND ----------
# Latest simulation cube metadata
cubes = sorted(f for f in os.listdir(CUBE_DIR) if f.startswith("cube_") and f.endswith(".npz"))
with np.load(f"{CUBE_DIR}/{cubes[-1]}") as z:
    meta = json.loads(bytes(z["meta"]).decode())
    t_years = z["t"].astype(float)
    x_state = {k.split("__", 1)[1]: z[k].astype(np.float64) for k in z.files if k.startswith("x__")}
sim_as_of = dt.date.fromisoformat(meta["as_of"][:10])
print(f"cube {cubes[-1]}: as_of {sim_as_of}, {len(meta['dates'])} steps, {meta['n_paths']} paths")

# COMMAND ----------
# ref_currency
cur = spark.sql(f"""
    SELECT ccy, MIN(as_of_date) AS first_curve_date, MAX(as_of_date) AS last_curve_date,
           COUNT(DISTINCT as_of_date) AS n_curve_dates
    FROM {T('gold_zero_curves')} GROUP BY ccy""").toPandas()
cur["ccy_name"] = cur.ccy.map(CCY_NAMES)
cur["hw_alpha"] = cur.ccy.map(lambda c: float(meta["hw"][c][0]) if c in meta["hw"] else None)
cur["hw_sigma"] = cur.ccy.map(lambda c: float(meta["hw"][c][1]) if c in meta["hw"] else None)
cur["is_simulated"] = cur.ccy.isin(list(meta["hw"]))
cur["sim_as_of_date"] = sim_as_of
cur["n_curve_dates"] = cur.n_curve_dates.astype("int32")
write(cur[["ccy", "ccy_name", "first_curve_date", "last_curve_date", "n_curve_dates", "is_simulated",
           "hw_alpha", "hw_sigma", "sim_as_of_date"]].sort_values("ccy"), "ref_currency")

# COMMAND ----------
# ref_tenor
ten = pd.DataFrame([dict(tenor_label=k, tenor_years=v, on_curve_grid=True) for k, v in GRID.items()] +
                   [dict(tenor_label=k, tenor_years=v, on_curve_grid=False) for k, v in OFF_GRID.items()])
ten = ten.sort_values("tenor_years").reset_index(drop=True)
ten["sort_order"] = np.arange(1, len(ten) + 1, dtype="int32")
write(ten, "ref_tenor")

# COMMAND ----------
# ref_rate: every pickable simulated rate
rows = []
for ccy in sorted(meta["hw"]):
    rows.append(dict(rate_id=f"{ccy}_SHORT", ccy=ccy, rate_type="SHORT", tenor_label=SHORT_RATE_LABEL, tenor_years=0.0,
                     rate_source="HW1F short rate r(t) = x(t) + phi(t) (simulated state)",
                     on_curve_grid=False, sort_order=0,
                     display_name=f"{ccy} short rate"))
    for _, r in ten.iterrows():
        src = ("Zero rate on the bootstrap grid, from P(t,T | x(t)) on the reconstructed curve" if r.on_curve_grid
               else "Off-grid zero rate read off the reconstructed curve P(t,T | x(t))")
        rows.append(dict(rate_id=f"{ccy}_Z{r.tenor_label}", ccy=ccy, rate_type="ZERO",
                         tenor_label=r.tenor_label, tenor_years=float(r.tenor_years), rate_source=src,
                         on_curve_grid=bool(r.on_curve_grid), sort_order=int(r.sort_order),
                         display_name=f"{ccy} {r.tenor_label} zero rate"))
rate = pd.DataFrame(rows)
rate["sort_order"] = rate.sort_order.astype("int32")
write(rate, "ref_rate")

# COMMAND ----------
# ref_curve_date
cd = spark.sql(f"""SELECT ccy, as_of_date, COUNT(*) AS n_tenors FROM {T('gold_zero_curves')}
                   GROUP BY ccy, as_of_date""").toPandas()
cd["n_tenors"] = cd.n_tenors.astype("int32")
write(cd.sort_values(["ccy", "as_of_date"]), "ref_curve_date")

# COMMAND ----------
# ref_sim_step
ss = pd.DataFrame({"sim_as_of_date": sim_as_of, "time_step": np.arange(len(t_years), dtype="int32"),
                   "sim_date": [dt.date.fromisoformat(d[:10]) for d in meta["dates"]], "t_years": t_years})
ss["days_ahead"] = [(d - sim_as_of).days for d in ss.sim_date]
ss["days_ahead"] = ss.days_ahead.astype("int32")
ss["n_paths"] = np.int32(meta["n_paths"])
write(ss, "ref_sim_step")

# COMMAND ----------
# ref_equity
eq = spark.sql(f"""SELECT ticker, FIRST(ccy) AS ccy, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                          COUNT(*) AS n_obs FROM {T('bronze_equity_prices')} GROUP BY ticker""").toPandas()
eq["n_obs"] = eq.n_obs.astype("int32")
write(eq.sort_values("ticker"), "ref_equity")

# COMMAND ----------
# ref_sim_curve: the initial curve of the latest simulation (nodes of riskcore ZeroCurve)
sc = pd.DataFrame([dict(sim_as_of_date=sim_as_of, ccy=c, tenor_label=lab, tenor_years=float(TENORS[lab]), zero_rate=float(v))
                   for c, zs in meta["zeros"].items() for lab, v in zs.items() if v is not None])
write(sc.sort_values(["ccy", "tenor_years"]), "ref_sim_curve")

# COMMAND ----------
# sim_rate_state: HW1F state x(t) for every currency, step and path of the latest simulation
n_s, n_p = len(t_years), int(meta["n_paths"])
st = pd.concat([pd.DataFrame({"sim_as_of_date": sim_as_of, "ccy": c,
                              "time_step": np.repeat(np.arange(n_s, dtype="int32"), n_p),
                              "path_id": np.tile(np.arange(n_p, dtype="int32"), n_s),
                              "x": arr.ravel()}) for c, arr in sorted(x_state.items())], ignore_index=True)
write(st, "sim_rate_state")

# COMMAND ----------
# History tables synced as-is need Change Data Feed for TRIGGERED sync
for tbl in ["gold_zero_curves", "gold_hw_params", "bronze_fx_rates", "bronze_equity_prices"]:
    spark.sql(f"ALTER TABLE {T(tbl)} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
print("CDF enabled on history tables")
