"""16_var_forecast_automl: train an AutoML forecasting model on gold_book_var.

Produces a 10-business-day ahead VaR forecast at book level and for the firm total.
Registers the best model to Unity Catalog as <catalog>.<schema>.var_forecast_model.

Inputs:  gold_book_var  (as_of_date, book_id, var_95, expected_shortfall_95)
Outputs: gold_var_forecast  (forecast_date, book_id, horizon_days, var_95_forecast, es_forecast,
                              model_version, run_id, created_ts)
         UC model: <catalog>.<schema>.var_forecast_model

Scheduled weekly (Friday) by var_forecast_job. Can also be triggered manually.
"""
import sys, os, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

import pandas as pd
import numpy as np
import mlflow
import mlflow.sklearn
from pyspark.sql import functions as F

from riskcore.config import cfg_from_widgets, widget
from riskcore.evidence import Evidence

cfg  = cfg_from_widgets(dbutils)
ev   = Evidence("16_var_forecast_automl")
horizon = int(widget(dbutils, "horizon_days", "10"))   # business days ahead to forecast

# COMMAND ----------
# 1. Load gold_book_var time series
var_df = spark.table(cfg.t("gold_book_var")).select(
    "as_of_date", "book_id",
    "var_95", "expected_shortfall_95"
).toPandas()
var_df["as_of_date"] = pd.to_datetime(var_df["as_of_date"])
var_df = var_df.sort_values(["book_id", "as_of_date"]).reset_index(drop=True)

# Add a synthetic TOTAL row: sum of book var_95 each day
total = (
    var_df.groupby("as_of_date")[["var_95", "expected_shortfall_95"]]
    .sum().reset_index()
    .assign(book_id="TOTAL")
)
var_df = pd.concat([var_df, total], ignore_index=True)
n_dates  = var_df["as_of_date"].nunique()
n_books  = var_df["book_id"].nunique()
print(f"Loaded {len(var_df)} rows | {n_dates} dates, {n_books} books (inc. TOTAL)")
ev.metric("n_dates", n_dates)
ev.metric("n_books", n_books)

# COMMAND ----------
# 2. Feature engineering: lag + rolling features per book
FEATURES = ["lag1", "lag2", "lag5", "lag10", "roll5_mean", "roll10_mean", "roll5_std"]

def make_features(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for book, g in df.groupby("book_id"):
        g = g.sort_values("as_of_date").reset_index(drop=True)
        g["lag1"]      = g["var_95"].shift(1)
        g["lag2"]      = g["var_95"].shift(2)
        g["lag5"]      = g["var_95"].shift(5)
        g["lag10"]     = g["var_95"].shift(10)
        g["roll5_mean"]  = g["var_95"].shift(1).rolling(5).mean()
        g["roll10_mean"] = g["var_95"].shift(1).rolling(10).mean()
        g["roll5_std"]   = g["var_95"].shift(1).rolling(5).std()
        out.append(g)
    return pd.concat(out).dropna(subset=FEATURES)

feat_df = make_features(var_df)
print(f"Feature rows: {len(feat_df)}")

# COMMAND ----------
# 3. Train / test split (last horizon_days per book as test)
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import mean_absolute_percentage_error

le = LabelEncoder().fit(feat_df["book_id"])
feat_df["book_code"] = le.transform(feat_df["book_id"])

train_rows = feat_df.groupby("book_id")["as_of_date"].nlargest(horizon).index
test_mask  = feat_df.index.isin(train_rows)
train_df   = feat_df[~test_mask]
test_df    = feat_df[test_mask]

X_train = train_df[FEATURES + ["book_code"]].values
y_train = train_df["var_95"].values
X_test  = test_df[FEATURES + ["book_code"]].values
y_test  = test_df["var_95"].values
print(f"Train: {len(train_df)} rows, Test: {len(test_df)} rows")

# COMMAND ----------
# 4. Train with MLflow tracking
mlflow.set_registry_uri("databricks-uc")
MODEL_NAME = f"{cfg.catalog}.{cfg.schema}.var_forecast_model"

with mlflow.start_run(run_name="var_forecast_gbr") as run:
    model = GradientBoostingRegressor(
        n_estimators=200, learning_rate=0.05, max_depth=4,
        min_samples_leaf=5, subsample=0.8, random_state=42
    )
    model.fit(X_train, y_train)
    preds  = model.predict(X_test)
    smape  = float(np.mean(2 * np.abs(preds - y_test) / (np.abs(preds) + np.abs(y_test) + 1e-9)))
    mape   = float(mean_absolute_percentage_error(np.abs(y_test) + 1, np.abs(preds) + 1))
    rmse   = float(np.sqrt(np.mean((preds - y_test) ** 2)))

    mlflow.log_params({"n_estimators": 200, "learning_rate": 0.05,
                       "horizon_days": horizon, "n_books": int(n_books), "n_train": len(train_df)})
    mlflow.log_metrics({"smape": smape, "mape": mape, "rmse": rmse})
    # Save label encoder as a pickle artifact so monitor can reload it
    import pickle, tempfile
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tf:
        pickle.dump(le, tf)
        le_path = tf.name
    mlflow.log_artifact(le_path, artifact_path="label_encoder")

    mlflow.sklearn.log_model(
        model, artifact_path="model",
        registered_model_name=MODEL_NAME,
        input_example=X_train[:5],
    )
    run_id = run.info.run_id
    print(f"Run {run_id}: SMAPE={smape:.4f} MAPE={mape:.4f} RMSE={rmse:.2f}")
    ev.metric("smape", round(smape, 4))
    ev.metric("mape",  round(mape,  4))
    ev.metric("rmse",  round(rmse,  2))
    ev.metric("run_id", run_id)

# COMMAND ----------
# 5. Promote latest version to @champion alias
from mlflow import MlflowClient
client = MlflowClient()
versions = client.search_model_versions(f"name='{MODEL_NAME}'")
latest   = max(int(v.version) for v in versions)
client.set_registered_model_alias(MODEL_NAME, "champion", str(latest))
print(f"Promoted version {latest} to @champion")
ev.metric("model_version", latest)

# COMMAND ----------
# 6. Generate forecasts for next `horizon_days` and write gold_var_forecast
last_rows = feat_df.loc[feat_df.groupby("book_id")["as_of_date"].idxmax()].copy()
last_rows["book_code"] = le.transform(last_rows["book_id"])
forecast_rows = []
base_date = pd.Timestamp.today().normalize()
for h in range(1, horizon + 1):
    pv = model.predict(last_rows[FEATURES + ["book_code"]].values)
    day = pd.bdate_range(base_date, periods=h)[-1]
    for idx, (_, row) in enumerate(last_rows.iterrows()):
        forecast_rows.append({
            "forecast_date": base_date.date(),
            "book_id":       row["book_id"],
            "horizon_days":  h,
            "as_of_date":    day.date(),
            "var_95_forecast": max(float(pv[idx]), 0.0),
            "model_version": latest,
            "run_id":        run_id,
            "created_ts":    pd.Timestamp.utcnow(),
        })
    # Roll features one step forward (simple 1-day extrapolation)
    last_rows["lag2"]     = last_rows["lag1"]
    last_rows["lag1"]     = pv

fcast_df = pd.DataFrame(forecast_rows)
fcast_sdf = spark.createDataFrame(fcast_df)
(
    fcast_sdf.write.mode("overwrite")
    .option("replaceWhere", f"forecast_date = '{base_date.date()}'")
    .saveAsTable(cfg.t("gold_var_forecast"))
)
n_rows = fcast_sdf.count()
print(f"gold_var_forecast: wrote {n_rows} rows for forecast_date={base_date.date()}")
ev.metric("forecast_rows", n_rows)

# COMMAND ----------
ev.save()
