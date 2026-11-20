-- Lakebase riskapp: writable app configuration schema (Postgres). Idempotent; run by src/jobs/configure_serving.py.
-- Reproduces production riskapp_cfg as read from its catalog on 2026-11-17.
-- Grants to the app service principal are issued per statement by configure_serving.py (so one failure is
-- reported rather than aborting the batch).
CREATE SCHEMA IF NOT EXISTS riskapp_cfg;

CREATE TABLE IF NOT EXISTS riskapp_cfg.tenor_config (
  tenor_label   text PRIMARY KEY CHECK (tenor_label ~ '^[0-9]+[MY]$'),
  tenor_years   double precision NOT NULL UNIQUE CHECK (tenor_years > 0 AND tenor_years <= 50),
  on_curve_grid boolean NOT NULL DEFAULT false,
  is_active     boolean NOT NULL DEFAULT true,
  updated_by    text NOT NULL DEFAULT CURRENT_USER,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (abs(tenor_years - (CASE WHEN right(tenor_label, 1) = 'M' THEN left(tenor_label, -1)::numeric / 12
                                 ELSE left(tenor_label, -1)::numeric END)::double precision) < 1e-9)
);

CREATE TABLE IF NOT EXISTS riskapp_cfg.tenor_config_audit (
  change_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenor_label     text NOT NULL,
  action          text NOT NULL CHECK (action IN ('ADD', 'EDIT', 'REMOVE', 'RESTORE', 'DELETE')),
  old_tenor_label text, old_tenor_years double precision, old_is_active boolean,
  new_tenor_label text, new_tenor_years double precision, new_is_active boolean,
  changed_by      text NOT NULL,
  changed_at      timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION riskapp_cfg.tenor_config_guard() RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE who text := coalesce(nullif(current_setting('riskapp.user', true), ''), current_user);
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.on_curve_grid THEN RAISE EXCEPTION 'Tenor % is on the bootstrap grid and cannot be removed', OLD.tenor_label; END IF;
    RETURN OLD;
  END IF;
  IF TG_OP = 'UPDATE' AND OLD.on_curve_grid THEN
    IF NEW.tenor_label <> OLD.tenor_label OR NEW.tenor_years <> OLD.tenor_years OR NOT NEW.is_active OR NOT NEW.on_curve_grid THEN
      RAISE EXCEPTION 'Tenor % is on the bootstrap grid and cannot be edited or removed', OLD.tenor_label;
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' AND NOT OLD.on_curve_grid AND NEW.on_curve_grid THEN
    RAISE EXCEPTION 'on_curve_grid can only be set by the platform seed, not edited';
  END IF;
  IF NEW.is_active AND (TG_OP = 'INSERT' OR NOT OLD.is_active) AND
     (SELECT count(*) FROM riskapp_cfg.tenor_config WHERE is_active) >= 40 THEN
    RAISE EXCEPTION 'At most 40 active tenors are allowed';
  END IF;
  NEW.updated_by := who; NEW.updated_at := now();
  RETURN NEW;
END $function$;

CREATE OR REPLACE FUNCTION riskapp_cfg.tenor_config_audit_fn() RETURNS trigger LANGUAGE plpgsql
  SECURITY DEFINER SET search_path TO 'riskapp_cfg', 'pg_temp' AS $function$
DECLARE who text := coalesce(nullif(current_setting('riskapp.user', true), ''), current_user);
        act text;
BEGIN
  IF TG_OP = 'INSERT' THEN act := 'ADD';
  ELSIF TG_OP = 'DELETE' THEN act := 'DELETE';
  ELSIF OLD.is_active AND NOT NEW.is_active THEN act := 'REMOVE';
  ELSIF NOT OLD.is_active AND NEW.is_active THEN act := 'RESTORE';
  ELSE act := 'EDIT'; END IF;
  INSERT INTO riskapp_cfg.tenor_config_audit (tenor_label, action, old_tenor_label, old_tenor_years, old_is_active,
                                              new_tenor_label, new_tenor_years, new_is_active, changed_by)
  VALUES (coalesce(NEW.tenor_label, OLD.tenor_label), act,
          CASE WHEN TG_OP <> 'INSERT' THEN OLD.tenor_label END, CASE WHEN TG_OP <> 'INSERT' THEN OLD.tenor_years END,
          CASE WHEN TG_OP <> 'INSERT' THEN OLD.is_active END,
          CASE WHEN TG_OP <> 'DELETE' THEN NEW.tenor_label END, CASE WHEN TG_OP <> 'DELETE' THEN NEW.tenor_years END,
          CASE WHEN TG_OP <> 'DELETE' THEN NEW.is_active END, who);
  RETURN NULL;
END $function$;

DROP TRIGGER IF EXISTS tenor_config_guard ON riskapp_cfg.tenor_config;
CREATE TRIGGER tenor_config_guard BEFORE INSERT OR DELETE OR UPDATE ON riskapp_cfg.tenor_config
  FOR EACH ROW EXECUTE FUNCTION riskapp_cfg.tenor_config_guard();
DROP TRIGGER IF EXISTS tenor_config_audit ON riskapp_cfg.tenor_config;
CREATE TRIGGER tenor_config_audit AFTER INSERT OR DELETE OR UPDATE ON riskapp_cfg.tenor_config
  FOR EACH ROW EXECUTE FUNCTION riskapp_cfg.tenor_config_audit_fn();

CREATE OR REPLACE VIEW riskapp_cfg.active_tenors AS
  SELECT tenor_label, tenor_years, on_curve_grid,
         row_number() OVER (ORDER BY tenor_years)::integer AS sort_order, updated_by, updated_at
  FROM riskapp_cfg.tenor_config WHERE is_active;
