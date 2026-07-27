-- =============================================================================
-- 010_autobid.sql — Auto-Bid Rules Engine data model (inst:autobid)
--
-- Declarative bidding rules with dual-control approval, immutable versioning,
-- and a full execution audit trail. Commercially gated by the AUTOBID module
-- entitlement (009_entitlements.sql; enforced in portal-api).
--
--   autobid.rule           lifecycle container (draft → pending_approval →
--                          active ⇄ paused → retired) + deterministic priority
--   autobid.rule_version   IMMUTABLE predicate/action/guardrails snapshots;
--                          every edit = new version = new approval cycle
--   autobid.execution      append-only log of every evaluation outcome
--                          (including no_match/suppressed — full explainability),
--                          monthly RANGE partitions
--   autobid.exposure       per-institution live exposure counter row, locked
--                          FOR UPDATE by the worker so concurrent evaluations
--                          cannot jointly breach caps (same discipline as the
--                          atomic PII reveal)
--
-- Design rules honoured:
--   * institution_id NOT NULL everywhere; RLS ENABLE + FORCE
--   * tenant policies via institution.current_member_ctx_v2()
--   * composite indexes lead with institution_id
--   * executions written ONLY by the worker/service role (no tenant writes)
--   * predicates are closed-vocabulary JSON trees validated in portal-api
--     (app/core/autobid_rules.py) — never user-supplied code
--
-- Also extends institution.approval_template.entity_type with 'autobid_rule'
-- so institutions can later route rule approval through configurable chains
-- (007 engine). The simple dual-control path ships first (same coexistence
-- posture as 007 itself).
--
-- Additive only; re-runnable.
-- =============================================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS autobid;

-- ---------- 1. Rules (lifecycle container) ----------
CREATE TABLE IF NOT EXISTS autobid.rule (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id      uuid NOT NULL,
  name                text NOT NULL,
  description         text,
  status              text NOT NULL DEFAULT 'draft'
                      CHECK (status IN
                        ('draft','pending_approval','active','paused','rejected','retired')),
  priority            int  NOT NULL DEFAULT 100 CHECK (priority BETWEEN 1 AND 10000),
  current_version_id  uuid,          -- FK added below (circular with rule_version)
  created_by          uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (institution_id, name)
);
CREATE INDEX IF NOT EXISTS idx_ab_rule_inst_status
  ON autobid.rule (institution_id, status);
-- Deterministic conflict resolution scan: active rules by priority.
CREATE INDEX IF NOT EXISTS idx_ab_rule_inst_priority
  ON autobid.rule (institution_id, priority, created_at) WHERE status = 'active';

-- ---------- 2. Immutable rule versions ----------
CREATE TABLE IF NOT EXISTS autobid.rule_version (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_id          uuid NOT NULL REFERENCES autobid.rule(id),
  institution_id   uuid NOT NULL,
  version_no       int  NOT NULL CHECK (version_no >= 1),
  predicate        jsonb NOT NULL,   -- closed-vocabulary condition tree
  action           jsonb NOT NULL,   -- {apr_pct, fee_schedule, lane, bid_validity_hours}
  guardrails       jsonb NOT NULL,   -- {max_bids_per_day, max_principal_per_day_mur,
                                     --  min_seconds_between_bids, expires_at?}
  submitted_by     uuid,             -- maker (stamped at submit)
  submitted_at     timestamptz,
  approved_by      uuid,             -- checker (stamped at approve; must differ)
  approved_at      timestamptz,
  rejected_by      uuid,
  rejected_at      timestamptz,
  rejection_note   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (rule_id, version_no),
  CHECK (approved_by IS NULL OR submitted_by IS NULL OR approved_by <> submitted_by)
);
CREATE INDEX IF NOT EXISTS idx_ab_rv_inst_rule
  ON autobid.rule_version (institution_id, rule_id, version_no DESC);

-- Circular FK: rule.current_version_id → rule_version.id
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'rule_current_version_fk'
  ) THEN
    ALTER TABLE autobid.rule
      ADD CONSTRAINT rule_current_version_fk
      FOREIGN KEY (current_version_id) REFERENCES autobid.rule_version(id);
  END IF;
END $$;

-- Immutability guard: approved versions can only receive approval/rejection
-- stamps; predicate/action/guardrails are frozen once submitted.
CREATE OR REPLACE FUNCTION autobid.rule_version_freeze()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.submitted_at IS NOT NULL AND (
       NEW.predicate  IS DISTINCT FROM OLD.predicate  OR
       NEW.action     IS DISTINCT FROM OLD.action     OR
       NEW.guardrails IS DISTINCT FROM OLD.guardrails OR
       NEW.version_no IS DISTINCT FROM OLD.version_no OR
       NEW.rule_id    IS DISTINCT FROM OLD.rule_id
  ) THEN
    RAISE EXCEPTION 'autobid.rule_version is immutable once submitted (version %)',
      OLD.version_no
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_rule_version_freeze') THEN
    CREATE TRIGGER trg_rule_version_freeze
      BEFORE UPDATE ON autobid.rule_version
      FOR EACH ROW EXECUTE FUNCTION autobid.rule_version_freeze();
  END IF;
END $$;

-- ---------- 3. Execution log (append-only, monthly partitions) ----------
CREATE TABLE IF NOT EXISTS autobid.execution (
  id               bigint GENERATED ALWAYS AS IDENTITY,
  institution_id   uuid NOT NULL,
  rule_id          uuid NOT NULL,
  rule_version_id  uuid NOT NULL,
  loan_request_id  uuid NOT NULL,
  outcome          text NOT NULL CHECK (outcome IN
                     ('bid_placed','suppressed_guardrail','suppressed_exposure',
                      'no_match','error')),
  bid_id           uuid,
  detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key  text NOT NULL,
  occurred_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, occurred_at),
  UNIQUE (idempotency_key, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE INDEX IF NOT EXISTS idx_ab_exec_inst_time
  ON autobid.execution (institution_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ab_exec_inst_request
  ON autobid.execution (institution_id, loan_request_id);

CREATE OR REPLACE FUNCTION autobid.ensure_execution_partition(p_month date)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = autobid, pg_temp
AS $$
DECLARE
  part_start date := date_trunc('month', p_month)::date;
  part_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
  part_name  text := format('execution_%s', to_char(part_start, 'YYYY_MM'));
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'autobid' AND c.relname = part_name
  ) THEN
    EXECUTE format(
      'CREATE TABLE autobid.%I PARTITION OF autobid.execution
         FOR VALUES FROM (%L) TO (%L)',
      part_name, part_start, part_end
    );
  END IF;
END $$;

CREATE OR REPLACE FUNCTION autobid.ensure_execution_partitions()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = autobid, pg_temp
AS $$
BEGIN
  PERFORM autobid.ensure_execution_partition(current_date);
  PERFORM autobid.ensure_execution_partition((current_date + interval '1 month')::date);
  PERFORM autobid.ensure_execution_partition((current_date + interval '2 months')::date);
END $$;

SELECT autobid.ensure_execution_partitions();

-- ---------- 4. Live exposure counters (worker-locked) ----------
CREATE TABLE IF NOT EXISTS autobid.exposure (
  institution_id        uuid PRIMARY KEY,
  open_bid_count        int     NOT NULL DEFAULT 0 CHECK (open_bid_count >= 0),
  open_principal_mur    numeric(16,2) NOT NULL DEFAULT 0 CHECK (open_principal_mur >= 0),
  bids_today            int     NOT NULL DEFAULT 0 CHECK (bids_today >= 0),
  principal_today_mur   numeric(16,2) NOT NULL DEFAULT 0 CHECK (principal_today_mur >= 0),
  today                 date    NOT NULL DEFAULT current_date,
  updated_at            timestamptz NOT NULL DEFAULT now()
);

-- ---------- 5. RLS ----------
-- rule / rule_version: tenant read-write (writes further constrained by the
-- portal-api lifecycle endpoints; DB enforces isolation + immutability).
-- execution / exposure: tenant READ-only; writes are worker/service role only.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['rule','rule_version','execution','exposure'] LOOP
    EXECUTE format('ALTER TABLE autobid.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE autobid.%I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='autobid'
                 AND tablename='rule' AND policyname='ab_rule_tenant') THEN
    CREATE POLICY ab_rule_tenant ON autobid.rule
      USING (institution_id = (SELECT ctx.institution_id
                               FROM institution.current_member_ctx_v2() ctx))
      WITH CHECK (institution_id = (SELECT ctx.institution_id
                                    FROM institution.current_member_ctx_v2() ctx));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='autobid'
                 AND tablename='rule_version' AND policyname='ab_rv_tenant') THEN
    CREATE POLICY ab_rv_tenant ON autobid.rule_version
      USING (institution_id = (SELECT ctx.institution_id
                               FROM institution.current_member_ctx_v2() ctx))
      WITH CHECK (institution_id = (SELECT ctx.institution_id
                                    FROM institution.current_member_ctx_v2() ctx));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='autobid'
                 AND tablename='execution' AND policyname='ab_exec_tenant_read') THEN
    CREATE POLICY ab_exec_tenant_read ON autobid.execution
      FOR SELECT
      USING (institution_id = (SELECT ctx.institution_id
                               FROM institution.current_member_ctx_v2() ctx));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='autobid'
                 AND tablename='exposure' AND policyname='ab_exposure_tenant_read') THEN
    CREATE POLICY ab_exposure_tenant_read ON autobid.exposure
      FOR SELECT
      USING (institution_id = (SELECT ctx.institution_id
                               FROM institution.current_member_ctx_v2() ctx));
  END IF;
END $$;

-- ---------- 6. Approval-engine entity type (phase-2 routing hook) ----------
DO $$
DECLARE conname text;
BEGIN
  SELECT c.conname INTO conname
  FROM pg_constraint c
  JOIN pg_class r ON r.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = r.relnamespace
  WHERE n.nspname = 'institution' AND r.relname = 'approval_template'
    AND c.contype = 'c'
    AND pg_get_constraintdef(c.oid) LIKE '%entity_type%';

  IF conname IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_constraint c2
       JOIN pg_class r2 ON r2.oid = c2.conrelid
       WHERE r2.relname = 'approval_template'
         AND pg_get_constraintdef(c2.oid) LIKE '%autobid_rule%'
     )
  THEN
    EXECUTE format('ALTER TABLE institution.approval_template DROP CONSTRAINT %I', conname);
    ALTER TABLE institution.approval_template
      ADD CONSTRAINT approval_template_entity_type_check
      CHECK (entity_type IN
        ('bid','offer_letter','countersign','investment_mandate',
         'api_key','webhook','user_admin','autobid_rule','custom'));
  END IF;
END $$;

COMMIT;
