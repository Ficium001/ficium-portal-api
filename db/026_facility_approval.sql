-- =============================================================================
-- 026_facility_approval.sql — credit approval of facilities through the
-- approval engine (committees, delegation-of-authority rules).
--
-- WHY
--   The approval engine (db/007) was configured but never started: nothing
--   called approvals_route(), approval_instance had 0 rows ever, and the
--   "Board / Management Approval" deal stage was a plain two-person click with
--   no limits, no committee and no reject. Deals could not be declined at all.
--
--   Also broken in the engine: approvals_is_eligible() read
--   institution.user_role, which does not exist, so any 'single', 'dual',
--   'checklist' or 'external_hold' stage errored on every vote. Only committee
--   stages could ever be decided.
--
-- WHAT
--   1. 'facility' is an approval entity type.
--   2. pipeline_stage_def.approval_route: the stage is decided by the engine.
--      Submitting it routes the deal through the institution's DoA rules.
--      pipeline_stage_instance.approval_instance_id links the two.
--   3. Trigger on approval_instance: the engine's outcome drives the deal —
--        approved            -> stage completes, next stage activates (or deal completes)
--        rejected            -> stage blocked, deal DECLINED with the rejection comment
--        escalated           -> re-routed to the escalation template, stays in approval
--        expired / withdrawn -> stage back to active, officer resubmits
--      Runs for every path that resolves an instance (votes, SLA job, withdraw).
--   4. marketplace.close_deal(): decline (bank) or withdraw (borrower) a deal
--      with a reason; withdraws any approval in progress.
--   5. approvals_is_eligible(): single/dual/... stages check institution.member
--      (active, same institution, member_role = approver_role when set).
--   6. Defaults so nothing regresses: every institution with a
--      'board_approval' stage gets a "Credit approval" template (one checker,
--      48h SLA) + a catch-all facility DoA rule, and those stages are switched
--      to approval_route. Institutions refine with committees/limits after.
--   7. Columns loan_pipeline.closed_reason / closed_at / closed_by; notification
--      kind 'deal_closed'.
-- =============================================================================

BEGIN;

-- 1 ── entity type ─────────────────────────────────────────────────────────────
ALTER TABLE institution.approval_template DROP CONSTRAINT IF EXISTS approval_template_entity_type_check;
ALTER TABLE institution.approval_template ADD CONSTRAINT approval_template_entity_type_check CHECK (entity_type = ANY (ARRAY[
    'bid', 'offer_letter', 'countersign', 'investment_mandate', 'api_key', 'webhook',
    'user_admin', 'autobid_rule', 'custom', 'facility'
]));

-- 2 ── linking columns ─────────────────────────────────────────────────────────
ALTER TABLE institution.pipeline_stage_def
    ADD COLUMN IF NOT EXISTS approval_route boolean NOT NULL DEFAULT false;
ALTER TABLE marketplace.pipeline_stage_instance
    ADD COLUMN IF NOT EXISTS approval_instance_id uuid REFERENCES institution.approval_instance(id);
CREATE INDEX IF NOT EXISTS ix_stage_instance_approval
    ON marketplace.pipeline_stage_instance (approval_instance_id) WHERE approval_instance_id IS NOT NULL;

ALTER TABLE marketplace.loan_pipeline
    ADD COLUMN IF NOT EXISTS closed_reason text,
    ADD COLUMN IF NOT EXISTS closed_at     timestamptz,
    ADD COLUMN IF NOT EXISTS closed_by     uuid;

ALTER TABLE public.portal_notifications DROP CONSTRAINT IF EXISTS portal_notifications_kind_check;
ALTER TABLE public.portal_notifications ADD CONSTRAINT portal_notifications_kind_check CHECK (kind = ANY (ARRAY[
    'bid_accepted', 'bid_rejected', 'bid_expired', 'request_closed', 'pipeline_advanced',
    'pipeline_approved', 'approval_needed', 'compliance_expiring', 'system', 'deal_booked',
    'deal_closed'
]));

-- 5 ── eligibility fix ─────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION institution.approvals_is_eligible(p_stage_instance uuid, p_actor uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'institution', 'public'
AS $function$
DECLARE v record;
BEGIN
  SELECT si.id, sd.stage_type, sd.committee_id, sd.approver_role, i.institution_id
    INTO v
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  JOIN approval_instance i ON i.id = si.instance_id
  WHERE si.id = p_stage_instance;
  IF NOT FOUND THEN RETURN false; END IF;

  IF v.stage_type = 'committee' THEN
    RETURN EXISTS (
      SELECT 1 FROM committee_member cm
      WHERE cm.committee_id = v.committee_id AND cm.member_id = p_actor
        AND cm.is_voting AND cm.valid_from <= current_date
        AND (cm.valid_to IS NULL OR cm.valid_to >= current_date));
  ELSIF v.stage_type IN ('single','dual','checklist','external_hold') THEN
    -- Was institution.user_role (never existed). Approvers are identified by
    -- auth uid, as everywhere else in the engine.
    RETURN EXISTS (
      SELECT 1 FROM institution.member m
      WHERE m.auth_user_id = p_actor AND m.institution_id = v.institution_id AND m.active
        AND (v.approver_role IS NULL OR m.member_role = v.approver_role));
  END IF;
  RETURN false;
END $function$;

-- 3 ── outcome -> deal ─────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION marketplace.on_facility_approval_resolved()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
AS $fn$
DECLARE
  s        marketplace.pipeline_stage_instance%ROWTYPE;
  d        marketplace.loan_pipeline%ROWTYPE;
  v_next   uuid;
  v_reason text;
  v_esc    uuid;
  v_new    uuid;
BEGIN
  SELECT * INTO s FROM marketplace.pipeline_stage_instance
   WHERE approval_instance_id = NEW.id FOR UPDATE;
  IF NOT FOUND THEN RETURN NEW; END IF;
  SELECT * INTO d FROM marketplace.loan_pipeline WHERE id = s.pipeline_id FOR UPDATE;
  -- Deal already closed (e.g. declined while in approval), or stage moved on.
  IF d.status <> 'active' OR s.status <> 'awaiting_approval' THEN RETURN NEW; END IF;

  IF NEW.status = 'approved' THEN
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'completed', approved_at = now(), completed_at = now(), updated_at = now()
     WHERE id = s.id;
    SELECT id INTO v_next FROM marketplace.pipeline_stage_instance
     WHERE pipeline_id = s.pipeline_id AND position > s.position AND status = 'pending'
     ORDER BY position LIMIT 1;
    IF v_next IS NOT NULL THEN
      UPDATE marketplace.pipeline_stage_instance
         SET status = 'active', started_at = now(), updated_at = now() WHERE id = v_next;
      UPDATE marketplace.loan_pipeline SET current_stage_id = v_next, updated_at = now() WHERE id = d.id;
    ELSE
      UPDATE marketplace.loan_pipeline
         SET status = 'completed', completed_at = now(), updated_at = now() WHERE id = d.id;
    END IF;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'pipeline_approved', 'Credit approval granted',
            'The facility was approved and the deal moved to its next stage.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', NEW.id));

  ELSIF NEW.status = 'rejected' THEN
    SELECT a.comment INTO v_reason
      FROM institution.approval_action a
      JOIN institution.approval_stage_instance si ON si.id = a.stage_instance_id
     WHERE si.instance_id = NEW.id AND a.action = 'reject'
     ORDER BY a.created_at DESC LIMIT 1;
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'blocked', rejection_reason = v_reason, updated_at = now() WHERE id = s.id;
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'skipped', updated_at = now()
     WHERE pipeline_id = d.id AND status = 'pending';
    UPDATE marketplace.loan_pipeline
       SET status = 'declined', closed_reason = COALESCE(v_reason, 'Credit approval rejected.'),
           closed_at = now(), updated_at = now()
     WHERE id = d.id;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'deal_closed', 'Credit approval rejected',
            'The facility was rejected and the deal is declined.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', NEW.id));

  ELSIF NEW.status = 'escalated' THEN
    SELECT sd.escalate_to_template_id INTO v_esc
      FROM institution.approval_stage_instance si
      JOIN institution.approval_stage_def sd ON sd.id = si.stage_def_id
     WHERE si.instance_id = NEW.id AND si.status = 'expired'
     ORDER BY si.seq DESC LIMIT 1;
    IF v_esc IS NOT NULL THEN
      v_new := institution.approvals_route(NEW.institution_id, 'facility', NEW.entity_id,
                 NEW.entity_maker_id, NEW.entity_snapshot, v_esc, NEW.doa_rule_id);
      UPDATE marketplace.pipeline_stage_instance
         SET approval_instance_id = v_new, updated_at = now() WHERE id = s.id;
      INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
      VALUES (d.institution_id, 'approval_needed', 'Credit approval escalated',
              'The approval passed its deadline and was escalated.',
              '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', v_new));
    ELSE
      UPDATE marketplace.pipeline_stage_instance
         SET status = 'active', updated_at = now() WHERE id = s.id;
    END IF;

  ELSE  -- expired, withdrawn: back to the officer to resubmit
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'active', updated_at = now() WHERE id = s.id;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'approval_needed',
            CASE NEW.status WHEN 'expired' THEN 'Credit approval expired' ELSE 'Credit approval withdrawn' END,
            'Resubmit the facility for credit approval from the deal.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', NEW.id));
  END IF;
  RETURN NEW;
END $fn$;

DROP TRIGGER IF EXISTS trg_facility_approval_resolved ON institution.approval_instance;
CREATE TRIGGER trg_facility_approval_resolved
    AFTER UPDATE OF status ON institution.approval_instance
    FOR EACH ROW
    WHEN (NEW.entity_type = 'facility' AND OLD.status = 'in_progress' AND NEW.status <> 'in_progress')
    EXECUTE FUNCTION marketplace.on_facility_approval_resolved();

-- 4 ── close a deal ────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION marketplace.close_deal(
    p_pipeline_id uuid, p_institution_id uuid, p_outcome text, p_reason text, p_actor uuid
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
AS $fn$
DECLARE d marketplace.loan_pipeline%ROWTYPE; v_inst uuid;
BEGIN
  IF p_outcome NOT IN ('declined', 'withdrawn') THEN RAISE EXCEPTION 'DEAL_OUTCOME_INVALID'; END IF;
  IF p_reason IS NULL OR char_length(btrim(p_reason)) < 5 THEN RAISE EXCEPTION 'DEAL_REASON_REQUIRED'; END IF;
  SELECT * INTO d FROM marketplace.loan_pipeline
   WHERE id = p_pipeline_id AND institution_id = p_institution_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'DEAL_NOT_FOUND'; END IF;
  IF d.status <> 'active' THEN RAISE EXCEPTION 'DEAL_NOT_ACTIVE'; END IF;

  UPDATE marketplace.loan_pipeline
     SET status = p_outcome::pipeline_status_enum, closed_reason = btrim(p_reason),
         closed_at = now(), closed_by = p_actor, updated_at = now()
   WHERE id = d.id;

  -- Stop any approval still running. The deal is no longer active, so the
  -- outcome trigger leaves it alone.
  FOR v_inst IN
    SELECT psi.approval_instance_id FROM marketplace.pipeline_stage_instance psi
      JOIN institution.approval_instance ai ON ai.id = psi.approval_instance_id
     WHERE psi.pipeline_id = d.id AND ai.status = 'in_progress'
  LOOP
    PERFORM institution.approvals_withdraw(v_inst, p_actor, 'Deal ' || p_outcome || ': ' || btrim(p_reason));
  END LOOP;

  UPDATE marketplace.pipeline_stage_instance
     SET status = 'skipped', updated_at = now()
   WHERE pipeline_id = d.id AND status IN ('pending', 'active', 'awaiting_approval');

  INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
  VALUES (d.institution_id, 'deal_closed',
          CASE p_outcome WHEN 'declined' THEN 'Deal declined' ELSE 'Deal withdrawn' END,
          btrim(p_reason), '/deals/' || d.id,
          jsonb_build_object('pipeline_id', d.id, 'outcome', p_outcome));

  RETURN jsonb_build_object('pipeline_id', d.id, 'status', p_outcome, 'reason', btrim(p_reason));
END $fn$;

REVOKE ALL ON FUNCTION marketplace.close_deal(uuid, uuid, text, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION marketplace.on_facility_approval_resolved() FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.close_deal(uuid, uuid, text, text, uuid) FROM anon, authenticated';
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.on_facility_approval_resolved() FROM anon, authenticated';
  END IF;
END $$;

-- 6 ── defaults: no regression for existing approval stages ────────────────────
DO $$
DECLARE r record; v_admin uuid; v_tpl uuid;
BEGIN
  FOR r IN
    SELECT DISTINCT sd.institution_id
      FROM institution.pipeline_stage_def sd
     WHERE sd.stage_key = 'board_approval'
       AND NOT EXISTS (SELECT 1 FROM institution.doa_rule dr
                        WHERE dr.institution_id = sd.institution_id
                          AND dr.entity_type = 'facility' AND dr.status = 'active')
  LOOP
    SELECT m.auth_user_id INTO v_admin FROM institution.member m
     WHERE m.institution_id = r.institution_id AND m.active
     ORDER BY m.is_primary_admin DESC NULLS LAST, m.created_at LIMIT 1;

    INSERT INTO institution.approval_template
        (institution_id, name, entity_type, version, status, created_by, approved_by)
    VALUES (r.institution_id, 'Credit approval (default)', 'facility', 1, 'active', v_admin, v_admin)
    RETURNING id INTO v_tpl;

    INSERT INTO institution.approval_stage_def
        (template_id, seq, name, stage_type, approver_role, sla_hours, on_sla_breach, config)
    VALUES (v_tpl, 1, 'Credit approval', 'single', 'checker', 48, 'notify', '{}'::jsonb);

    INSERT INTO institution.doa_rule
        (institution_id, entity_type, priority, conditions, template_id, status, created_by)
    VALUES (r.institution_id, 'facility', 1000, '{}'::jsonb, v_tpl, 'active', v_admin);
  END LOOP;

  UPDATE institution.pipeline_stage_def SET approval_route = true
   WHERE stage_key = 'board_approval' AND NOT approval_route;
END $$;

COMMIT;
