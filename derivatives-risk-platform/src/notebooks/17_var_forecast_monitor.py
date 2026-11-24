"""17_var_forecast_monitor: weekly model quality gate for the VaR forecast model.

Checks whether the @champion model's out-of-sample SMAPE (scored on the last `eval_days`
of actuals) has degraded by more than `max_degradation` (default 10%) relative to the
SMAPE logged when that model version was trained.  If degradation is detected it triggers
a full retrain by running 16_var_forecast_automl as a downstream notebook task.

Inputs:  UC model var_forecast_model @champion
         gold_book_var actuals
Outputs: gold_var_forecast_monitor  (check_ts, model_version, baseline_smape, current_smape,
                                      degraded, retrained, notes)
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import numpy as np
import pandas as pd
import mlflow
import mlflow.sklearn
from mlflow import MlflowClient
from pyspark.sql import functions as F

from riskcore.config import cfg_from_widgets, widget
from riskcore.evidence import Evidence

cfg             = cfg_from_widgets(dbutils)
ev              = Evidence("17_var_forecast_monitor")
eval_days       = int(widget(dbutils, "eval_days",       "21"))  # actuals lookback for scoring
max_degradation = float(widget(dbutils, "max_degradation", "0.10"))  # 10% threshold
horizon         = int(widget(dbutils, "horizon_days",    "10"))

mlflow.set_registry_uri("databricks-uc")
MODEL_NAME = f"{cfg.catalog}.{cfg.schema}.var_forecast_model"

# COMMAND ----------
# 1. Load @champion model and its baseline SMAPE
client  = MlflowClient()
aliases = client.get_model_version_by_alias(MODEL_NAME, "champion")
champ_v = aliases.version
champ_run_id = aliases.run_id
baseline_smape = float(client.get_run(champ_run_id).data.metrics.get("smape", np.nan))
print(f"Champion: version={champ_v}  baseline_smape={baseline_smape:.4f}")
ev.metric("champion_version",  champ_v)
ev.metric("baseline_smape",    round(baseline_smape, 4))

# COMMAND ----------
# 2. Score on recent actuals
var_df = spark.table(cfg.t("gold_book_var")).select(
    "as_of_date", "book_id", "var_95"
).toPandas()
var_df["as_of_date"] = pd.to_datetime(var_df["as_of_date"])

# Add TOTAL
total = var_df.groupby("as_of_date")["var_95"].sum().reset_index().assign(book_id="TOTAL")
var_df = pd.concat([var_df, total], ignore_index=True)

cutoff = var_df["as_of_date"].max() - pd.Timedelta(days=eval_days)
held   = var_df[var_df["as_of_date"] > cutoff].copy()
print(f"Held-out actuals: {len(held)} rows from {held['as_of_date'].min().date()} to {held['as_of_date'].max().date()}")

# Feature engineering (same logic as notebook 16)
FEATURES = ["lag1", "lag2", "lag5", "lag10", "roll5_mean", "roll10_mean", "roll5_std"]

def make_features(df):
    out = []
    for book, g in df.groupby("book_id"):
        g = g.sort_values("as_of_date").reset_index(drop=True)
        g["lag1"]        = g["var_95"].shift(1)
        g["lag2"]        = g["var_95"].shift(2)
        g["lag5"]        = g["var_95"].shift(5)
        g["lag10"]       = g["var_95"].shift(10)
        g["roll5_mean"]  = g["var_95"].shift(1).rolling(5).mean()
        g["roll10_mean"] = g["var_95"].shift(1).rolling(10).mean()
        g["roll5_std"]   = g["var_95"].shift(1).rolling(5).std()
        out.append(g)
    return pd.concat(out).dropna(subset=FEATURES)

all_feat   = make_features(var_df)
held_feat  = all_feat[all_feat["as_of_date"] > cutoff]

import pickle, tempfile, urllib.request, pathlib
# Load label encoder artifact from the champion run
artifact_uri = client.get_run(champ_run_id).info.artifact_uri
le_path = mlflow.artifacts.download_artifacts(
    run_id=champ_run_id, artifact_path="label_encoder"
)
import glob
le_pkl = glob.glob(f"{le_path}/**/*.pkl", recursive=True)[0]
with open(le_pkl, "rb") as f:
    le = pickle.load(f)

# Load model
model_uri = f"models:/{MODEL_NAME}@champion"
model = mlflow.sklearn.load_model(model_uri)

try:
    held_feat["book_code"] = le.transform(held_feat["book_id"])
except ValueError as e:
    # New books introduced since the model was trained
    print(f"WARN: {e}; dropping unknown books from scoring")
    known = set(le.classes_)
    held_feat = held_feat[held_feat["book_id"].isin(known)].copy()
    held_feat["book_code"] = le.transform(held_feat["book_id"])

preds = model.predict(held_feat[FEATURES + ["book_code"]].values)
y_act = held_feat["var_95"].values
current_smape = float(np.mean(2 * np.abs(preds - y_act) / (np.abs(preds) + np.abs(y_act) + 1e-9)))
print(f"Current SMAPE={current_smape:.4f}  Baseline={baseline_smape:.4f}  Threshold={baseline_smape*(1+max_degradation):.4f}")
ev.metric("current_smape",   round(current_smape, 4))
ev.metric("degradation_pct", round((current_smape - baseline_smape) / max(baseline_smape, 1e-9), 4))

# COMMAND ----------
# 3. Degradation check
degraded = current_smape > baseline_smape * (1 + max_degradation)
retrained = False
notes = f"baseline={baseline_smape:.4f} current={current_smape:.4f} max_allowed={baseline_smape*(1+max_degradation):.4f}"

if degraded:
    print(f"DEGRADATION DETECTED ({current_smape:.4f} > {baseline_smape*(1+max_degradation):.4f}): triggering retrain")
    # Trigger 16_var_forecast_automl as a downstream notebook step
    # In the job DAG, the monitor task has retrain_var_forecast as a dependent run_if=FAILED task
    # but we also trigger it directly via dbutils for in-process retraining
    try:
        dbutils.notebook.run(
            "./16_var_forecast_automl",
            timeout_seconds=3600,
            arguments={"catalog": cfg.catalog, "schema": cfg.schema,
                       "horizon_days": str(horizon)},
        )
        retrained = True
        notes += "  RETRAINED=true"
    except Exception as e:
        notes += f"  RETRAIN_ERROR={e}"
        print(f"Retrain failed: {e}")
else:
    print(f"Model healthy (SMAPE={current_smape:.4f} <= {baseline_smape*(1+max_degradation):.4f})")

# COMMAND ----------
# 4. Write monitor result to gold_var_forecast_monitor
mon_df = pd.DataFrame([{
    "check_ts":        pd.Timestamp.utcnow(),
    "model_version":   int(champ_v),
    "baseline_smape":  round(baseline_smape, 6),
    "current_smape":   round(current_smape, 6),
    "degraded":        degraded,
    "retrained":       retrained,
    "notes":           notes,
}])
mon_sdf = spark.createDataFrame(mon_df)
mon_sdf.write.mode("append").saveAsTable(cfg.t("gold_var_forecast_monitor"))
print(f"Monitor result written to gold_var_forecast_monitor: degraded={degraded}")
ev.metric("degraded",  degraded)
ev.metric("retrained", retrained)
ev.save()
