-- =============================================================================
-- 029_credit_decision.sql — credit decisions irrespective of where they're made.
--
-- WHY
--   db/026 made the credit stage decidable only by Ficium's approval engine.
--   Institutions that run credit in their own system (core banking, LOS,
--   a paper committee) had no way to carry that decision into the deal, and
--   the engine's vocabulary was approve/reject only — no conditions, no
--   refer-back.
--
-- WHAT
--   1. pipeline_stage_def.decision_source  'internal' | 'external'
--        internal  the stage routes through Ficium's approval engine (db/026)
--        external  the decision is made elsewhere and recorded here, by the
--                  bank's system over /v1 (API key) or by a credit officer in
--                  the portal. Set per workflow, so per product.
--   2. marketplace.credit_decision — ONE history of decisions for both modes.
--        outcome   approved | approved_with_conditions | referred_back | declined
--        source    engine | portal | api
--      The engine trigger (db/026) now writes a row too, so the deal page and
--      the webhook read one table whatever the mode.
--   3. marketplace.record_credit_decision() — every rule for external
--      decisions, so the portal and API channels behave identically:
--        approved / approved_with_conditions  stage completes, next activates
--                                             (or the deal completes)
--        referred_back                        stage stays active with the reason
--        declined                             deal declined with the reason
--      An external decision is refused on an internal stage, so a bank system
--      cannot bypass the committee.
-- =============================================================================

BEGIN;

-- 1 ── mode per stage ──────────────────────────────────────────────────────────
ALTER TABLE institution.pipeline_stage_def
    ADD COLUMN IF NOT EXISTS decision_source text NOT NULL DEFAULT 'internal';
ALTER TABLE institution.pipeline_stage_def
    DROP CONSTRAINT IF EXISTS pipeline_stage_def_decision_source_check;
ALTER TABLE institution.pipeline_stage_def
    ADD CONSTRAINT pipeline_stage_def_decision_source_check
    CHECK (decision_source IN ('internal', 'external'));

-- 2 ── decision history ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS marketplace.credit_decision (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id          uuid NOT NULL REFERENCES marketplace.loan_pipeline(id),
    institution_id       uuid NOT NULL,
    stage_instance_id    uuid NOT NULL REFERENCES marketplace.pipeline_stage_instance(id),
    source               text NOT NULL CHECK (source IN ('engine', 'portal', 'api')),
    outcome              text NOT NULL CHECK (outcome IN
                           ('approved', 'approved_with_conditions', 'referred_back', 'declined')),
    conditions           jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason               text,
    external_reference   text,
    decided_at           timestamptz NOT NULL DEFAULT now(),
    recorded_by          uuid,
    api_key_id           uuid,
    approval_instance_id uuid REFERENCES institution.approval_instance(id),
    created_at           timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(conditions) = 'array'),
    CHECK (outcome <> 'approved_with_conditions' OR jsonb_array_length(conditions) > 0),
    CHECK (outcome NOT IN ('declined', 'referred_back') OR reason IS NOT NULL),
    CHECK (source = 'engine' OR external_reference IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_credit_decision_pipeline
    ON marketplace.credit_decision (pipeline_id, created_at DESC);

ALTER TABLE marketplace.credit_decision ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS credit_decision_tenant ON marketplace.credit_decision;
CREATE POLICY credit_decision_tenant ON marketplace.credit_decision
    FOR SELECT TO authenticated
    USING (institution_id = (SELECT institution_id FROM institution.current_member_ctx_v2()));
-- Read-only to tenants: decisions are written only through the functions below.
-- Explicit grant: the missing-GRANT class of bug has bitten twice (finance,
-- doc_template). RLS without a grant is a 500, not a filter.
GRANT SELECT ON marketplace.credit_decision TO authenticated;
REVOKE INSERT, UPDATE, DELETE ON marketplace.credit_decision FROM authenticated;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON marketplace.credit_decision FROM anon';
  END IF;
END $$;

-- 3 ── record an external decision ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION marketplace.record_credit_decision(
    p_pipeline_id    uuid,
    p_institution_id uuid,
    p_outcome        text,
    p_conditions     jsonb,
    p_reason         text,
    p_reference      text,
    p_decided_at     timestamptz,
    p_source         text,
    p_actor          uuid,
    p_api_key        uuid
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
AS $fn$
DECLARE
  d       marketplace.loan_pipeline%ROWTYPE;
  s       record;
  v_next  uuid;
  v_id    uuid;
  v_conds jsonb := COALESCE(p_conditions, '[]'::jsonb);
  v_deal  text  := 'active';
BEGIN
  IF p_source NOT IN ('portal', 'api') THEN RAISE EXCEPTION 'CREDIT_SOURCE_INVALID'; END IF;
  IF p_outcome NOT IN ('approved', 'approved_with_conditions', 'referred_back', 'declined') THEN
    RAISE EXCEPTION 'CREDIT_OUTCOME_INVALID';
  END IF;
  IF p_reference IS NULL OR char_length(btrim(p_reference)) NOT BETWEEN 1 AND 64 THEN
    RAISE EXCEPTION 'CREDIT_REFERENCE_INVALID';
  END IF;
  IF jsonb_typeof(v_conds) <> 'array' THEN RAISE EXCEPTION 'CREDIT_CONDITIONS_INVALID'; END IF;
  IF p_outcome = 'approved_with_conditions' AND jsonb_array_length(v_conds) = 0 THEN
    RAISE EXCEPTION 'CREDIT_CONDITIONS_REQUIRED';
  END IF;
  IF p_outcome <> 'approved_with_conditions' AND jsonb_array_length(v_conds) > 0 THEN
    RAISE EXCEPTION 'CREDIT_CONDITIONS_UNEXPECTED';
  END IF;
  IF p_outcome IN ('declined', 'referred_back')
     AND (p_reason IS NULL OR char_length(btrim(p_reason)) < 5) THEN
    RAISE EXCEPTION 'CREDIT_REASON_REQUIRED';
  END IF;
  IF p_decided_at IS NOT NULL AND p_decided_at > now() + interval '5 minutes' THEN
    RAISE EXCEPTION 'CREDIT_DATE_INVALID';
  END IF;

  SELECT * INTO d FROM marketplace.loan_pipeline
   WHERE id = p_pipeline_id AND institution_id = p_institution_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'CREDIT_DEAL_NOT_FOUND'; END IF;
  IF d.status <> 'active' THEN RAISE EXCEPTION 'CREDIT_DEAL_NOT_ACTIVE'; END IF;
  IF p_decided_at IS NOT NULL AND p_decided_at < d.created_at THEN
    RAISE EXCEPTION 'CREDIT_DATE_INVALID';
  END IF;

  -- The credit stage is the deal's current active approval stage.
  SELECT psi.id, psi.position, psd.decision_source INTO s
    FROM marketplace.pipeline_stage_instance psi
    JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
   WHERE psi.pipeline_id = d.id AND psi.status = 'active' AND psd.approval_route
   ORDER BY psi.position LIMIT 1
   FOR UPDATE OF psi;
  IF NOT FOUND THEN RAISE EXCEPTION 'CREDIT_STAGE_NOT_ACTIVE'; END IF;
  IF s.decision_source <> 'external' THEN RAISE EXCEPTION 'CREDIT_STAGE_INTERNAL'; END IF;

  INSERT INTO marketplace.credit_decision
      (pipeline_id, institution_id, stage_instance_id, source, outcome, conditions,
       reason, external_reference, decided_at, recorded_by, api_key_id)
  VALUES (d.id, d.institution_id, s.id, p_source, p_outcome, v_conds,
          NULLIF(btrim(p_reason), ''), btrim(p_reference), COALESCE(p_decided_at, now()),
          p_actor, p_api_key)
  RETURNING id INTO v_id;

  IF p_outcome IN ('approved', 'approved_with_conditions') THEN
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'completed', approved_at = now(), completed_at = now(),
           rejection_reason = NULL, updated_at = now()
     WHERE id = s.id;
    SELECT id INTO v_next FROM marketplace.pipeline_stage_instance
     WHERE pipeline_id = d.id AND position > s.position AND status = 'pending'
     ORDER BY position LIMIT 1;
    IF v_next IS NOT NULL THEN
      UPDATE marketplace.pipeline_stage_instance
         SET status = 'active', started_at = now(), updated_at = now() WHERE id = v_next;
      UPDATE marketplace.loan_pipeline SET current_stage_id = v_next, updated_at = now()
       WHERE id = d.id;
    ELSE
      UPDATE marketplace.loan_pipeline
         SET status = 'completed', completed_at = now(), updated_at = now() WHERE id = d.id;
      v_deal := 'completed';
    END IF;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'pipeline_approved',
            CASE p_outcome WHEN 'approved' THEN 'Credit approved'
                           ELSE 'Credit approved with conditions' END,
            'Credit reference ' || btrim(p_reference) || '.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'credit_decision_id', v_id));

  ELSIF p_outcome = 'referred_back' THEN
    UPDATE marketplace.pipeline_stage_instance
       SET rejection_reason = btrim(p_reason), updated_at = now() WHERE id = s.id;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'approval_needed', 'Credit referred back', btrim(p_reason),
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'credit_decision_id', v_id));

  ELSE  -- declined
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'blocked', rejection_reason = btrim(p_reason), updated_at = now()
     WHERE id = s.id;
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'skipped', updated_at = now()
     WHERE pipeline_id = d.id AND status = 'pending';
    UPDATE marketplace.loan_pipeline
       SET status = 'declined', closed_reason = btrim(p_reason), closed_at = now(),
           closed_by = p_actor, updated_at = now()
     WHERE id = d.id;
    v_deal := 'declined';
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'deal_closed', 'Credit declined', btrim(p_reason),
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'credit_decision_id', v_id));
  END IF;

  RETURN jsonb_build_object(
    'id', v_id, 'pipeline_id', d.id, 'stage_id', s.id, 'outcome', p_outcome,
    'conditions', v_conds, 'reason', NULLIF(btrim(p_reason), ''),
    'external_reference', btrim(p_reference),
    'decided_at', COALESCE(p_decided_at, now()), 'source', p_source,
    'deal_status', v_deal, 'next_stage_id', v_next);
END $fn$;

REVOKE ALL ON FUNCTION marketplace.record_credit_decision(
    uuid, uuid, text, jsonb, text, text, timestamptz, text, uuid, uuid) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.record_credit_decision(
        uuid, uuid, text, jsonb, text, text, timestamptz, text, uuid, uuid) FROM anon, authenticated';
  END IF;
END $$;

-- 4 ── engine decisions land in the same history ───────────────────────────────
-- Separate AFTER trigger rather than editing db/026's function: the outcome
-- logic stays where it is; this only records it. Fires on the same transition.
CREATE OR REPLACE FUNCTION marketplace.on_facility_approval_record_decision()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
AS $fn$
DECLARE v_stage uuid; v_pid uuid; v_reason text; v_actor uuid;
BEGIN
  IF NEW.status NOT IN ('approved', 'rejected') THEN RETURN NEW; END IF;
  SELECT psi.id, psi.pipeline_id INTO v_stage, v_pid
    FROM marketplace.pipeline_stage_instance psi WHERE psi.approval_instance_id = NEW.id;
  IF NOT FOUND THEN RETURN NEW; END IF;
  SELECT a.comment, a.actor_id INTO v_reason, v_actor
    FROM institution.approval_action a
    JOIN institution.approval_stage_instance si ON si.id = a.stage_instance_id
   WHERE si.instance_id = NEW.id
     AND a.action = CASE NEW.status WHEN 'approved' THEN 'approve' ELSE 'reject' END
   ORDER BY a.created_at DESC LIMIT 1;
  INSERT INTO marketplace.credit_decision
      (pipeline_id, institution_id, stage_instance_id, source, outcome, reason,
       decided_at, recorded_by, approval_instance_id)
  VALUES (v_pid, NEW.institution_id, v_stage, 'engine',
          CASE NEW.status WHEN 'approved' THEN 'approved' ELSE 'declined' END,
          COALESCE(v_reason, CASE NEW.status WHEN 'rejected' THEN 'Credit approval rejected.' END),
          COALESCE(NEW.resolved_at, now()), v_actor, NEW.id);
  RETURN NEW;
END $fn$;

REVOKE ALL ON FUNCTION marketplace.on_facility_approval_record_decision() FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.on_facility_approval_record_decision() FROM anon, authenticated';
  END IF;
END $$;

DROP TRIGGER IF EXISTS trg_facility_approval_record_decision ON institution.approval_instance;
CREATE TRIGGER trg_facility_approval_record_decision
    AFTER UPDATE OF status ON institution.approval_instance
    FOR EACH ROW
    WHEN (NEW.entity_type = 'facility' AND OLD.status = 'in_progress'
          AND NEW.status IN ('approved', 'rejected'))
    EXECUTE FUNCTION marketplace.on_facility_approval_record_decision();

COMMIT;
