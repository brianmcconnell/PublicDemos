-- Audit trail of what-if runs from risk-curves-app (Trade what-if tab), one row per run.
-- Headline numbers only (no simulation paths), the trade as priced, when it was calculated and who ran it.
-- run_by is masked for everyone outside the workspace `admins` group.
-- The app's service principal appends rows through a SQL warehouse (app resource "sql-warehouse").
-- Template: {{CATALOG}}, {{SCHEMA}} and {{APP_SP}} (app service principal client id) are filled in by
-- src/jobs/configure_serving.py, which runs the statements one by one (split on ';').

CREATE OR REPLACE FUNCTION {{CATALOG}}.{{SCHEMA}}.mask_whatif_run_by(run_by STRING)
RETURNS STRING
COMMENT 'Column mask for whatif_run_audit.run_by: visible to the workspace admins group, redacted for everyone else.'
RETURN CASE WHEN is_member('admins') THEN run_by
            WHEN run_by IS NULL THEN NULL
            ELSE '[masked]' END;

CREATE TABLE IF NOT EXISTS {{CATALOG}}.{{SCHEMA}}.whatif_run_audit (
  audit_id             STRING    NOT NULL COMMENT 'Unique id of the audit row (uuid)',
  calculated_at        TIMESTAMP NOT NULL COMMENT 'When the what-if was calculated (UTC, app clock)',
  logged_at            TIMESTAMP NOT NULL COMMENT 'When the row was written (UTC, warehouse clock)',
  run_by               STRING    COMMENT 'Email of the user who ran the what-if (masked for non-admins)',
  app_name             STRING    COMMENT 'Databricks App that ran the calculation',
  lakebase_run_id      BIGINT    COMMENT 'riskapp_whatif.runs.run_id in Lakebase (null if that write failed)',
  whatif_trade_id      STRING    NOT NULL COMMENT 'riskapp_whatif.trades.trade_id in Lakebase',
  trade_label          STRING    COMMENT 'Free-text label entered by the user',
  valuation_date       DATE      NOT NULL COMMENT 'Batch simulation valuation date the trade was priced on',
  sim_cube_file        STRING    COMMENT 'Simulation cube file in the sim_cubes volume',
  n_paths              INT       COMMENT 'Monte Carlo paths',
  product              STRING    NOT NULL COMMENT 'irs, xccy, fra, fxfwd, euro_opt, amer_opt',
  book_id              STRING    NOT NULL,
  counterparty_id      STRING    NOT NULL,
  netting_set_id       STRING,
  collateral_id        STRING,
  direction            STRING,
  ccy                  STRING    COMMENT 'Trade currency (ccy for options = underlying currency)',
  ccy_2                STRING    COMMENT 'Second currency (cross-currency swap)',
  underlying           STRING    COMMENT 'Equity underlying (options)',
  notional             DOUBLE    COMMENT 'Notional in trade currency units (options: quantity x strike)',
  rate_or_strike       DOUBLE    COMMENT 'Fixed rate (decimal), forward rate (USD per unit) or option strike',
  start_date           DATE,
  maturity_date        DATE,
  trade_details        STRING    COMMENT 'The priced trade (all pricer fields) as JSON',
  mtm_usd              DOUBLE    COMMENT 'Trade mark-to-market today (USD)',
  trade_var_1d         DOUBLE    COMMENT 'Stand-alone 1-day VaR95 of the trade',
  trade_es_1d          DOUBLE    COMMENT 'Stand-alone 1-day ES95 of the trade',
  book_var_1d_before   DOUBLE, book_var_1d_after   DOUBLE,
  book_es_1d_before    DOUBLE, book_es_1d_after    DOUBLE,
  book_var_10d_before  DOUBLE, book_var_10d_after  DOUBLE,
  book_es_10d_before   DOUBLE, book_es_10d_after   DOUBLE,
  firm_var_1d_before   DOUBLE, firm_var_1d_after   DOUBLE,
  firm_es_1d_before    DOUBLE, firm_es_1d_after    DOUBLE,
  cpty_netting_key     STRING    COMMENT 'Netting key the trade was added to (netting set, or trade-level when NA)',
  cpty_peak_pfe_before DOUBLE, cpty_peak_pfe_after DOUBLE,
  cpty_peak_ee_before  DOUBLE, cpty_peak_ee_after  DOUBLE,
  cpty_epe_1y_before   DOUBLE, cpty_epe_1y_after   DOUBLE,
  trade_peak_pfe       DOUBLE    COMMENT 'Stand-alone peak PFE95 of the trade',
  compute_seconds      DOUBLE    COMMENT 'Pricing + risk time in the app'
)
COMMENT 'Audit of Trade what-if runs from risk-curves-app: headline before/after VaR, ES, EE and PFE, the trade priced, when and by whom. run_by is masked for non-admins. Written by the app service principal.'
CLUSTER BY (valuation_date, book_id);

ALTER TABLE {{CATALOG}}.{{SCHEMA}}.whatif_run_audit
  ALTER COLUMN run_by SET MASK {{CATALOG}}.{{SCHEMA}}.mask_whatif_run_by;

-- App service principal (risk-curves-app): writes rows; the app never reads this table back
GRANT USE CATALOG ON CATALOG {{CATALOG}} TO `{{APP_SP}}`;
GRANT USE SCHEMA ON SCHEMA {{CATALOG}}.{{SCHEMA}} TO `{{APP_SP}}`;
GRANT MODIFY, SELECT ON TABLE {{CATALOG}}.{{SCHEMA}}.whatif_run_audit TO `{{APP_SP}}`;
