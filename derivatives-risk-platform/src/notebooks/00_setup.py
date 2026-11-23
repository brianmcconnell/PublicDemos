"""00_setup: create schema, tables, volumes and dimension tables.

Idempotent — safe to rerun. Uses CREATE TABLE IF NOT EXISTS throughout.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))
from riskcore.config import cfg_from_widgets, Cfg  # noqa: E402
from riskcore.evidence import Evidence  # noqa: E402

cfg = cfg_from_widgets(dbutils)
ev = Evidence("00_setup")
ev.h("Parameters")
ev.metric("catalog", cfg.catalog)
ev.metric("schema", cfg.schema)

# COMMAND ----------
# Schema
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cfg.catalog}.{cfg.schema}")
print(f"Schema {cfg.catalog}.{cfg.schema} ready")

# COMMAND ----------
# Volumes for trade files and simulation cubes
for vol_name in ["trade_files", "sim_cubes", "evidence"]:
    spark.sql(f"CREATE VOLUME IF NOT EXISTS {cfg.catalog}.{cfg.schema}.{vol_name}")
    print(f"Volume {cfg.catalog}.{cfg.schema}.{vol_name} ready")

# COMMAND ----------
# Bronze tables
for ddl in [
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('bronze_fred_rates')} (
        series_id STRING, obs_date DATE, value DOUBLE,
        category STRING, ccy STRING, tenor_label STRING, tenor_years DOUBLE, is_proxy BOOLEAN,
        load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('bronze_equity_prices')} (
        ticker STRING, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
        adj_close DOUBLE, volume BIGINT, ccy STRING, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('bronze_fx_rates')} (
        series_id STRING, obs_date DATE, value DOUBLE, ccy STRING, source STRING, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('bronze_bond_prices')} (
        isin STRING, trade_date DATE, clean_price DOUBLE, dirty_price DOUBLE, yield_to_maturity DOUBLE,
        ccy STRING, source STRING, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('bronze_bond_ref')} (
        isin STRING, issuer STRING, ccy STRING, coupon DOUBLE, maturity_date DATE, issue_date DATE,
        rating STRING, sector STRING, source STRING, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('bronze_credit_spreads')} (
        series_id STRING, obs_date DATE, value DOUBLE, rating STRING, tenor STRING, source STRING, load_ts TIMESTAMP)
    USING DELTA""",
]:
    spark.sql(ddl)
print("Bronze market tables ready")

# Trade bronze tables (one per file format)
for prod in ["irs", "xccy", "fra", "fxfwd", "euro_opt", "amer_opt"]:
    spark.sql(f"""CREATE TABLE IF NOT EXISTS {cfg.t(f'bronze_trades_{prod}')} (
        _raw STRING, _file STRING, _load_ts TIMESTAMP)
    USING DELTA""")
print("Bronze trade tables ready")

# COMMAND ----------
# Silver tables (typed market data)
for ddl in [
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_zero_rates')} (
        as_of_date DATE, ccy STRING, tenor_label STRING, tenor_years DOUBLE, zero_rate DOUBLE,
        source STRING, method STRING, is_proxy BOOLEAN, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_equity_prices')} (
        ticker STRING, trade_date DATE, adj_close DOUBLE, ccy STRING, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_fx_spot')} (
        ccy STRING, obs_date DATE, usd_per_ccy DOUBLE, source STRING, load_ts TIMESTAMP)
    USING DELTA""",
]:
    spark.sql(ddl)
print("Silver market tables ready")

# Silver trade tables (one per product) — full schema
for ddl in [
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_trades_irs')} (
        trade_id STRING, product STRING, book_id STRING, counterparty_id STRING,
        netting_set_id STRING, collateral_id STRING, version INT, as_of_date DATE,
        ccy STRING, notional DOUBLE, fixed_rate DOUBLE, float_index STRING,
        pay_freq_months INT, start_date DATE, maturity_date DATE,
        last_reset_date DATE, current_fixing DOUBLE, direction STRING, trade_date DATE,
        load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_trades_xccy')} (
        trade_id STRING, product STRING, book_id STRING, counterparty_id STRING,
        netting_set_id STRING, collateral_id STRING, version INT, as_of_date DATE,
        ccy STRING, ccy_2 STRING, notional DOUBLE, notional_2 DOUBLE,
        fx_rate_at_inception DOUBLE, fixed_rate DOUBLE, fixed_rate_2 DOUBLE,
        float_index STRING, pay_freq_months INT, start_date DATE, maturity_date DATE,
        direction STRING, trade_date DATE, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_trades_fra')} (
        trade_id STRING, product STRING, book_id STRING, counterparty_id STRING,
        netting_set_id STRING, collateral_id STRING, version INT, as_of_date DATE,
        ccy STRING, notional DOUBLE, fixed_rate DOUBLE, float_index STRING,
        start_date DATE, end_date DATE, maturity_date DATE, settlement_date DATE,
        direction STRING, trade_date DATE, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_trades_fxfwd')} (
        trade_id STRING, product STRING, book_id STRING, counterparty_id STRING,
        netting_set_id STRING, collateral_id STRING, version INT, as_of_date DATE,
        ccy_pair STRING, ccy STRING, quote_ccy STRING, notional DOUBLE,
        forward_rate DOUBLE, trade_date DATE, start_date DATE, maturity_date DATE,
        direction STRING, load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_trades_euro_opt')} (
        trade_id STRING, product STRING, book_id STRING, counterparty_id STRING,
        netting_set_id STRING, collateral_id STRING, version INT, as_of_date DATE,
        underlying STRING, ccy STRING, strike DOUBLE, quantity DOUBLE, notional DOUBLE,
        option_type STRING, direction STRING, exercise_style STRING,
        dividend_yield DOUBLE, trade_date DATE, start_date DATE, maturity_date DATE,
        load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('silver_trades_amer_opt')} (
        trade_id STRING, product STRING, book_id STRING, counterparty_id STRING,
        netting_set_id STRING, collateral_id STRING, version INT, as_of_date DATE,
        underlying STRING, ccy STRING, strike DOUBLE, quantity DOUBLE, notional DOUBLE,
        option_type STRING, direction STRING, exercise_style STRING,
        dividend_yield DOUBLE, trade_date DATE, start_date DATE, maturity_date DATE,
        load_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('quarantine_trades')} (
        trade_id STRING, product STRING, defect STRING, as_of_date DATE, load_ts TIMESTAMP)
    USING DELTA""",
]:
    spark.sql(ddl)
print("Silver trade tables ready")

# COMMAND ----------
# Gold tables
for ddl in [
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_zero_curves')} (
        as_of_date DATE, ccy STRING, tenor_label STRING, tenor_years DOUBLE, zero_rate DOUBLE,
        source STRING, method STRING, is_proxy BOOLEAN)
    USING DELTA PARTITIONED BY (as_of_date, ccy)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_hist_vol')} (
        calc_date DATE, factor_name STRING, return_type STRING, window STRING,
        ann_vol DOUBLE, n_obs INT)
    USING DELTA PARTITIONED BY (calc_date)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_corr_matrix')} (
        calc_date DATE, factor_i STRING, factor_j STRING, correlation DOUBLE)
    USING DELTA PARTITIONED BY (calc_date)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_hw_params')} (
        as_of_date DATE, ccy STRING, alpha DOUBLE, sigma DOUBLE,
        calibration_case INT, is_fallback BOOLEAN, vol_df1y DOUBLE, vol_df10y DOUBLE,
        vol_ratio DOUBLE, n_obs INT)
    USING DELTA PARTITIONED BY (as_of_date)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_sim_curves')} (
        as_of_date DATE, ccy STRING, time_step INT, path_id INT,
        tenor_label STRING, zero_rate DOUBLE)
    USING DELTA PARTITIONED BY (as_of_date, ccy)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_sim_fx')} (
        as_of_date DATE, ccy STRING, time_step INT, path_id INT, fx_rate DOUBLE)
    USING DELTA PARTITIONED BY (as_of_date)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_sim_equity')} (
        as_of_date DATE, ticker STRING, time_step INT, path_id INT, price DOUBLE)
    USING DELTA PARTITIONED BY (as_of_date)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_cpty_exposure')} (
        as_of_date DATE, counterparty_id STRING, netting_set_id STRING,
        time_step INT, n_trades INT, ee DOUBLE, pfe_95 DOUBLE,
        p5 DOUBLE, p25 DOUBLE, p50 DOUBLE, p75 DOUBLE, p95 DOUBLE)
    USING DELTA PARTITIONED BY (as_of_date)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_book_var')} (
        as_of_date DATE, book_id STRING, time_step INT, n_trades INT,
        t0_pv DOUBLE, mean_pv DOUBLE, p5_pv DOUBLE, p95_pv DOUBLE,
        var_95 DOUBLE, expected_shortfall_95 DOUBLE)
    USING DELTA PARTITIONED BY (as_of_date)""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('gold_trade_prices_sample')} (
        as_of_date DATE, trade_id STRING, product STRING, counterparty_id STRING,
        time_step INT, path_id INT, pv DOUBLE)
    USING DELTA PARTITIONED BY (as_of_date)""",
]:
    spark.sql(ddl)
print("Gold tables ready")

# COMMAND ----------
# Dimension tables
for ddl in [
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('dim_book')} (
        book_id STRING, desk STRING, business_unit STRING)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('dim_counterparty')} (
        counterparty_id STRING, name STRING, rating STRING, sector STRING, country STRING)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('dim_netting_agreement')} (
        netting_set_id STRING, counterparty_id STRING, agreement_type STRING,
        governing_law STRING, signed_date DATE)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('dim_collateral_agreement')} (
        collateral_id STRING, netting_set_id STRING, counterparty_id STRING,
        agreement_status STRING, csa_ccy STRING, threshold DOUBLE, mta DOUBLE, mpor_days INT)
    USING DELTA""",
]:
    spark.sql(ddl)
print("Dimension tables ready")

# COMMAND ----------
# Control tables
for ddl in [
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('ctl_load_watermark')} (
        source STRING, last_date DATE, updated_ts TIMESTAMP)
    USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {cfg.t('ctl_backfill_log')} (
        source STRING, chunk_start DATE, chunk_end DATE, status STRING,
        n_rows BIGINT, started_ts TIMESTAMP, finished_ts TIMESTAMP)
    USING DELTA""",
]:
    spark.sql(ddl)
print("Control tables ready")

# COMMAND ----------
# Evidence
tables = [r.tableName for r in spark.sql(f"SHOW TABLES IN {cfg.catalog}.{cfg.schema}").collect()]
ev.metric("tables created", len(tables))
ev.check("schema exists", len(tables) > 0, f"{len(tables)} tables")
ev.check(">= 30 tables", len(tables) >= 30, f"found {len(tables)}")
ev.table("Tables", spark.sql(f"SHOW TABLES IN {cfg.catalog}.{cfg.schema}").toPandas())
ev.save()
