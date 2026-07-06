-- =============================================================================
-- 009_entitlements.sql — Module entitlement & metered-usage layer (plat:billing)
--
-- Foundation for per-module commercial packaging (AUTOBID, SERVICING,
-- RATING_API, ...). Three concerns, deliberately separated:
--
--   entitlements.module                  platform-owned catalog (NOT tenant-scoped)
--   entitlements.institution_entitlement what each institution has bought
--   entitlements.usage_event             append-only metering ledger (partitioned)
--
-- Design rules honoured (see docs/adr):
--   * institution_id NOT NULL on every tenant-scoped table
--   * RLS ENABLE + FORCE; tenant SELECT-only via institution.current_member_ctx_v2()
--     — commercial state is written ONLY by the platform service role. Entitlement
--     is enforced in portal-api (app/core/entitlements.py), NOT encoded in RLS,
--     keeping billing state orthogonal to tenant isolation.
--   * composite indexes lead with institution_id
--   * usage ledger is append-only and idempotency-keyed (safe under retries)
--   * monthly RANGE partitions on usage_event; helper to create them ahead
--
-- Additive only; re-runnable (IF NOT EXISTS / guarded DO blocks).
-- =============================================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS entitlements;

-- ---------- 1. Module catalog (platform-owned; no RLS tenant policy) ----------
CREATE TABLE IF NOT EXISTS entitlements.module (
  module_code    text PRIMARY KEY
                 CHECK (module_code ~ '^[A-Z][A-Z0-9_]{1,39}$'),
  name           text NOT NULL,
  description    text,
  billing_model  text NOT NULL CHECK (billing_model IN ('flat','metered','flat_plus_metered')),
  metered_unit   text CHECK (
                   (billing_model = 'flat' AND metered_unit IS NULL) OR
                   (billing_model <> 'flat' AND metered_unit IS NOT NULL)),
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Read is harmless (it is a product catalog); writes are platform-only.
ALTER TABLE entitlements.module ENABLE ROW LEVEL SECURITY;
ALTER TABLE entitlements.module FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname = 'entitlements' AND tablename = 'module'
                   AND policyname = 'module_read_all') THEN
    CREATE POLICY module_read_all ON entitlements.module
      FOR SELECT USING (true);
  END IF;
END $$;

-- ---------- 2. Institution entitlements (tenant-scoped, SELECT-only) ----------
CREATE TABLE IF NOT EXISTS entitlements.institution_entitlement (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id  uuid NOT NULL,
  module_code     text NOT NULL REFERENCES entitlements.module(module_code),
  plan_tier       text NOT NULL DEFAULT 'standard'
                  CHECK (plan_tier IN ('trial','standard','professional','enterprise')),
  status          text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','trial','suspended','expired')),
  valid_from      timestamptz NOT NULL DEFAULT now(),
  valid_to        timestamptz,                       -- NULL = evergreen
  limits          jsonb NOT NULL DEFAULT '{}'::jsonb, -- e.g. {"max_active_rules": 50}
  created_by      uuid,                               -- platform operator, if any
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_to IS NULL OR valid_to > valid_from),
  UNIQUE (institution_id, module_code)
);
CREATE INDEX IF NOT EXISTS idx_ie_inst_module_status
  ON entitlements.institution_entitlement (institution_id, module_code, status);

ALTER TABLE entitlements.institution_entitlement ENABLE ROW LEVEL SECURITY;
ALTER TABLE entitlements.institution_entitlement FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname = 'entitlements'
                   AND tablename = 'institution_entitlement'
                   AND policyname = 'ie_tenant_read') THEN
    CREATE POLICY ie_tenant_read ON entitlements.institution_entitlement
      FOR SELECT USING (
        institution_id = (
          SELECT ctx.institution_id
          FROM institution.current_member_ctx_v2() ctx
        )
      );
  END IF;
END $$;
-- No tenant INSERT/UPDATE/DELETE policies on purpose: with FORCE RLS and no
-- write policy, only the platform service role (BYPASSRLS) can mutate rows.

-- ---------- 3. Usage ledger (append-only, monthly partitions) ----------
CREATE TABLE IF NOT EXISTS entitlements.usage_event (
  id               bigint GENERATED ALWAYS AS IDENTITY,
  institution_id   uuid NOT NULL,
  module_code      text NOT NULL,
  unit             text NOT NULL,
  quantity         numeric NOT NULL DEFAULT 1 CHECK (quantity > 0),
  idempotency_key  text NOT NULL,
  metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, occurred_at),
  UNIQUE (idempotency_key, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE INDEX IF NOT EXISTS idx_ue_inst_module_time
  ON entitlements.usage_event (institution_id, module_code, occurred_at);

ALTER TABLE entitlements.usage_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE entitlements.usage_event FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname = 'entitlements' AND tablename = 'usage_event'
                   AND policyname = 'ue_tenant_read') THEN
    CREATE POLICY ue_tenant_read ON entitlements.usage_event
      FOR SELECT USING (
        institution_id = (
          SELECT ctx.institution_id
          FROM institution.current_member_ctx_v2() ctx
        )
      );
  END IF;
END $$;

-- Partition helper: creates the monthly partition for a given date if missing.
-- Called by ensure_usage_partitions(); also safe to call from a cron worker.
CREATE OR REPLACE FUNCTION entitlements.ensure_usage_partition(p_month date)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = entitlements, pg_temp
AS $$
DECLARE
  part_start date := date_trunc('month', p_month)::date;
  part_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
  part_name  text := format('usage_event_%s', to_char(part_start, 'YYYY_MM'));
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'entitlements' AND c.relname = part_name
  ) THEN
    EXECUTE format(
      'CREATE TABLE entitlements.%I PARTITION OF entitlements.usage_event
         FOR VALUES FROM (%L) TO (%L)',
      part_name, part_start, part_end
    );
  END IF;
END $$;

-- Keep current month + 2 ahead provisioned (idempotent).
CREATE OR REPLACE FUNCTION entitlements.ensure_usage_partitions()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = entitlements, pg_temp
AS $$
BEGIN
  PERFORM entitlements.ensure_usage_partition(current_date);
  PERFORM entitlements.ensure_usage_partition((current_date + interval '1 month')::date);
  PERFORM entitlements.ensure_usage_partition((current_date + interval '2 months')::date);
END $$;

SELECT entitlements.ensure_usage_partitions();

-- ---------- 4. Seed the initial module catalog ----------
INSERT INTO entitlements.module (module_code, name, description, billing_model, metered_unit)
VALUES
  ('AUTOBID',    'Auto-Bid Rules Engine',
   'Declarative bidding rules with maker-checker approval, guardrails and kill switches.',
   'flat_plus_metered', 'executed_bid'),
  ('SERVICING',  'Post-Acceptance Servicing',
   'Loan lifecycle system of record: events, schedules, arrears, documents.',
   'metered', 'active_loan'),
  ('RATING_API', 'Rating-as-a-Service',
   'Standalone scored-decisioning API on the Ficium rating engine.',
   'metered', 'score_request')
ON CONFLICT (module_code) DO NOTHING;

COMMIT;
