-- =============================================================================
-- 007_approval_engine.sql — Configurable approval workflow engine (inst:approvals)
--
-- Generalizes maker-checker into institution-composable chains:
--   stage types: single | dual | committee (quorum) | checklist | external_hold
--   DoA routing by amount x risk x product x secured (evaluated in portal-api,
--   app/core/doa.py — the SAME evaluator serves /simulate and live routing).
--
-- COEXISTS with the existing governance.action maker-checker: nothing in this
-- file touches governance.*; existing flows are unchanged until explicitly
-- migrated onto the engine (Phase 2). Additive only; re-runnable.
--
-- Engine invariants validated on PostgreSQL 16 (7 SQL invariant tests +
-- 21-assertion HTTP walkthrough in the working model, 2026-07-04).
-- =============================================================================
BEGIN;

-- Tenant resolution: uses the existing institution.current_member_ctx_v2()
-- (same convention as 006_pipeline_rls.sql).

-- ---------- 1. Committees ----------
CREATE TABLE IF NOT EXISTS institution.approval_committee (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id  uuid NOT NULL,
  name            text NOT NULL,
  description     text,
  quorum_type     text NOT NULL CHECK (quorum_type IN ('count','fraction','unanimous')),
  quorum_value    numeric CHECK (
                    (quorum_type = 'count'    AND quorum_value >= 1) OR
                    (quorum_type = 'fraction' AND quorum_value > 0 AND quorum_value <= 1) OR
                    (quorum_type = 'unanimous' AND quorum_value IS NULL)),
  tie_break       text NOT NULL DEFAULT 'chair' CHECK (tie_break IN ('chair','reject','escalate')),
  allow_abstain   boolean NOT NULL DEFAULT true,
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  created_by      uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (institution_id, name)
);

CREATE TABLE IF NOT EXISTS institution.committee_member (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  committee_id  uuid NOT NULL REFERENCES institution.approval_committee(id),
  member_id     uuid NOT NULL,
  role          text NOT NULL DEFAULT 'member'
                CHECK (role IN ('chair','vice_chair','member','secretary','observer')),
  is_voting     boolean NOT NULL DEFAULT true,
  valid_from    date NOT NULL DEFAULT current_date,
  valid_to      date,
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_to IS NULL OR valid_to >= valid_from),
  UNIQUE (committee_id, member_id, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_cm_committee ON institution.committee_member(committee_id);

-- ---------- 2. Templates & stages ----------
CREATE TABLE IF NOT EXISTS institution.approval_template (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id  uuid NOT NULL,
  name            text NOT NULL,
  entity_type     text NOT NULL CHECK (entity_type IN
                  ('bid','offer_letter','countersign','investment_mandate',
                   'api_key','webhook','user_admin','custom')),
  version         int  NOT NULL DEFAULT 1,
  status          text NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','pending_activation','active','retired')),
  created_by      uuid NOT NULL,
  approved_by     uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (institution_id, name, version)
);

CREATE TABLE IF NOT EXISTS institution.approval_stage_def (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  template_id   uuid NOT NULL REFERENCES institution.approval_template(id),
  seq           int  NOT NULL CHECK (seq >= 1),
  name          text NOT NULL,
  stage_type    text NOT NULL CHECK (stage_type IN
                ('single','dual','committee','checklist','external_hold')),
  committee_id  uuid REFERENCES institution.approval_committee(id),
  approver_role text,
  checklist     jsonb,
  sla_hours     int CHECK (sla_hours IS NULL OR sla_hours > 0),
  on_sla_breach text NOT NULL DEFAULT 'notify'
                CHECK (on_sla_breach IN ('notify','escalate','auto_reject')),
  escalate_to_template_id uuid REFERENCES institution.approval_template(id),
  config        jsonb NOT NULL DEFAULT '{}',
  CHECK (stage_type <> 'committee' OR committee_id IS NOT NULL),
  CHECK (stage_type NOT IN ('single','dual') OR approver_role IS NOT NULL),
  CHECK (stage_type <> 'checklist' OR checklist IS NOT NULL),
  CHECK (on_sla_breach <> 'escalate' OR escalate_to_template_id IS NOT NULL),
  UNIQUE (template_id, seq)
);

-- ---------- 3. DoA rules ----------
CREATE TABLE IF NOT EXISTS institution.doa_rule (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id  uuid NOT NULL,
  entity_type     text NOT NULL,
  priority        int  NOT NULL,
  conditions      jsonb NOT NULL DEFAULT '{}',
  template_id     uuid NOT NULL REFERENCES institution.approval_template(id),
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  created_by      uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (institution_id, entity_type, priority)
);

-- ---------- 4. Runtime ----------
CREATE TABLE IF NOT EXISTS institution.approval_instance (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id   uuid NOT NULL,
  template_id      uuid NOT NULL REFERENCES institution.approval_template(id),
  template_version int  NOT NULL,
  doa_rule_id      uuid NOT NULL REFERENCES institution.doa_rule(id),
  entity_type      text NOT NULL,
  entity_id        uuid NOT NULL,
  entity_maker_id  uuid NOT NULL,
  entity_snapshot  jsonb NOT NULL,
  current_seq      int  NOT NULL DEFAULT 1,
  status           text NOT NULL DEFAULT 'in_progress' CHECK (status IN
                   ('in_progress','approved','rejected','expired','escalated','withdrawn')),
  withdraw_reason  text,
  started_at       timestamptz NOT NULL DEFAULT now(),
  resolved_at      timestamptz
);
-- one LIVE approval per entity (terminal states release the slot)
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_instance_per_entity
  ON institution.approval_instance (entity_type, entity_id)
  WHERE status = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_ai_inst_status
  ON institution.approval_instance (institution_id, status);

CREATE TABLE IF NOT EXISTS institution.approval_stage_instance (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  instance_id  uuid NOT NULL REFERENCES institution.approval_instance(id),
  seq          int  NOT NULL,
  stage_def_id uuid NOT NULL REFERENCES institution.approval_stage_def(id),
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN
               ('pending','active','approved','rejected','expired','skipped')),
  started_at   timestamptz,
  due_at       timestamptz,
  resolved_at  timestamptz,
  UNIQUE (instance_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_asi_due
  ON institution.approval_stage_instance (due_at) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS institution.approval_action (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage_instance_id uuid NOT NULL REFERENCES institution.approval_stage_instance(id),
  actor_id          uuid NOT NULL,
  acting_as         uuid,
  action            text NOT NULL CHECK (action IN
                    ('approve','reject','abstain','recuse','comment',
                     'checklist_update','delegate','withdraw','escalate')),
  comment           text,
  checklist_state   jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (action NOT IN ('reject','recuse') OR comment IS NOT NULL)
);
-- one decisive action per effective actor per stage
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_vote_per_actor
  ON institution.approval_action (stage_instance_id, COALESCE(acting_as, actor_id))
  WHERE action IN ('approve','reject','abstain');
CREATE INDEX IF NOT EXISTS idx_aa_stage ON institution.approval_action(stage_instance_id);

CREATE TABLE IF NOT EXISTS institution.approval_delegation (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id uuid NOT NULL,
  from_member    uuid NOT NULL,
  to_member      uuid NOT NULL,
  scope          text NOT NULL DEFAULT 'all',
  reason         text NOT NULL,
  valid_from     timestamptz NOT NULL,
  valid_to       timestamptz NOT NULL,
  approved_by    uuid NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (from_member <> to_member),
  CHECK (valid_to > valid_from)
);

-- outbox: decouples side effects (webhooks, bid release) from engine tx
CREATE TABLE IF NOT EXISTS institution.approval_outbox (
  id             bigserial PRIMARY KEY,
  institution_id uuid NOT NULL,
  event          text NOT NULL,
  payload        jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  processed_at   timestamptz
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
  ON institution.approval_outbox (id) WHERE processed_at IS NULL;

-- ---------- 5. RLS ----------
DO $rls$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['approval_committee','committee_member','approval_template',
    'approval_stage_def','doa_rule','approval_instance','approval_stage_instance',
    'approval_action','approval_delegation','approval_outbox']
  LOOP
    EXECUTE format('ALTER TABLE institution.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE institution.%I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $rls$;

DROP POLICY IF EXISTS apc_tenant ON institution.approval_committee;
CREATE POLICY apc_tenant ON institution.approval_committee
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS cm_tenant ON institution.committee_member;
CREATE POLICY cm_tenant ON institution.committee_member
  USING (committee_id IN (SELECT id FROM institution.approval_committee
                          WHERE institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)));
DROP POLICY IF EXISTS at_tenant ON institution.approval_template;
CREATE POLICY at_tenant ON institution.approval_template
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS asd_tenant ON institution.approval_stage_def;
CREATE POLICY asd_tenant ON institution.approval_stage_def
  USING (template_id IN (SELECT id FROM institution.approval_template
                         WHERE institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)));
DROP POLICY IF EXISTS doa_tenant ON institution.doa_rule;
CREATE POLICY doa_tenant ON institution.doa_rule
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS ai_tenant ON institution.approval_instance;
CREATE POLICY ai_tenant ON institution.approval_instance
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS asi_tenant ON institution.approval_stage_instance;
CREATE POLICY asi_tenant ON institution.approval_stage_instance
  USING (instance_id IN (SELECT id FROM institution.approval_instance
                         WHERE institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)));
DROP POLICY IF EXISTS aa_tenant ON institution.approval_action;
CREATE POLICY aa_tenant ON institution.approval_action
  USING (stage_instance_id IN (
    SELECT si.id FROM institution.approval_stage_instance si
    JOIN institution.approval_instance i ON i.id = si.instance_id
    WHERE i.institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)));
DROP POLICY IF EXISTS ad_tenant ON institution.approval_delegation;
CREATE POLICY ad_tenant ON institution.approval_delegation
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS ao_tenant ON institution.approval_outbox;
CREATE POLICY ao_tenant ON institution.approval_outbox
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));

-- append-only enforcement at grant level
REVOKE UPDATE, DELETE ON institution.approval_action FROM PUBLIC;
REVOKE UPDATE, DELETE ON institution.approval_action FROM authenticated;

-- ============================================================
-- ENGINE FUNCTIONS (SECURITY DEFINER, explicit tenant asserts)
-- DoA evaluation happens in portal-api (approvals/doa.py); the
-- resolved template_id + doa_rule_id are passed in — simulate
-- and live routing therefore share one code path by design.
-- ============================================================

CREATE OR REPLACE FUNCTION institution.approvals_route(
  p_institution_id uuid, p_entity_type text, p_entity_id uuid,
  p_entity_maker uuid, p_snapshot jsonb,
  p_template_id uuid, p_doa_rule_id uuid
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v_instance uuid; v_version int; v_stage record; v_first_due timestamptz;
BEGIN
  PERFORM 1 FROM approval_template
   WHERE id = p_template_id AND institution_id = p_institution_id AND status = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'APPROVALS_TEMPLATE_NOT_ACTIVE'; END IF;

  SELECT version INTO v_version FROM approval_template WHERE id = p_template_id;

  INSERT INTO approval_instance
    (institution_id, template_id, template_version, doa_rule_id,
     entity_type, entity_id, entity_maker_id, entity_snapshot)
  VALUES (p_institution_id, p_template_id, v_version, p_doa_rule_id,
          p_entity_type, p_entity_id, p_entity_maker, p_snapshot)
  RETURNING id INTO v_instance;

  FOR v_stage IN
    SELECT * FROM approval_stage_def WHERE template_id = p_template_id ORDER BY seq
  LOOP
    INSERT INTO approval_stage_instance (instance_id, seq, stage_def_id, status, started_at, due_at)
    VALUES (v_instance, v_stage.seq, v_stage.id,
            CASE WHEN v_stage.seq = 1 THEN 'active' ELSE 'pending' END,
            CASE WHEN v_stage.seq = 1 THEN now() END,
            CASE WHEN v_stage.seq = 1 AND v_stage.sla_hours IS NOT NULL
                 THEN now() + make_interval(hours => v_stage.sla_hours) END);
  END LOOP;

  INSERT INTO approval_outbox (institution_id, event, payload)
  VALUES (p_institution_id, 'approval.requested',
          jsonb_build_object('instance_id', v_instance, 'entity_type', p_entity_type,
                             'entity_id', p_entity_id));
  RETURN v_instance;
END $$;

-- eligibility of an actor for a stage (voting membership / role holder)
CREATE OR REPLACE FUNCTION institution.approvals_is_eligible(
  p_stage_instance uuid, p_actor uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
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
    -- role check: institution RBAC — adapt to existing user_role table
    RETURN EXISTS (
      SELECT 1 FROM institution.user_role ur
      WHERE ur.user_id = p_actor AND ur.institution_id = v.institution_id
        AND (v.approver_role IS NULL OR ur.role = v.approver_role));
  END IF;
  RETURN false;
END $$;

CREATE OR REPLACE FUNCTION institution.approvals_cast(
  p_stage_instance uuid, p_actor uuid, p_action text,
  p_comment text DEFAULT NULL, p_checklist jsonb DEFAULT NULL,
  p_acting_as uuid DEFAULT NULL
) RETURNS text  -- resulting stage status
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v record; v_effective uuid; v_result text;
BEGIN
  SELECT si.id AS stage_id, si.status AS stage_status, si.seq,
         sd.stage_type, sd.committee_id, sd.checklist AS checklist_def,
         i.id AS instance_id, i.status AS inst_status,
         i.entity_maker_id, i.institution_id
    INTO v
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  JOIN approval_instance i ON i.id = si.instance_id
  WHERE si.id = p_stage_instance
  FOR UPDATE OF si;

  IF NOT FOUND THEN RAISE EXCEPTION 'APPROVALS_STAGE_NOT_FOUND'; END IF;
  IF v.inst_status <> 'in_progress' OR v.stage_status <> 'active' THEN
    RAISE EXCEPTION 'APPROVALS_STAGE_NOT_ACTIVE';
  END IF;

  v_effective := COALESCE(p_acting_as, p_actor);

  -- delegation validity
  IF p_acting_as IS NOT NULL THEN
    PERFORM 1 FROM approval_delegation d
    WHERE d.institution_id = v.institution_id
      AND d.from_member = p_acting_as AND d.to_member = p_actor
      AND now() BETWEEN d.valid_from AND d.valid_to
      AND (d.scope = 'all' OR d.scope = v.committee_id::text
           OR d.scope IN (SELECT template_id::text FROM approval_instance WHERE id = v.instance_id));
    IF NOT FOUND THEN RAISE EXCEPTION 'APPROVALS_DELEGATION_INVALID'; END IF;
  END IF;

  IF p_action IN ('approve','reject','abstain') THEN
    -- recusal: maker never votes on own entity
    IF v_effective = v.entity_maker_id OR p_actor = v.entity_maker_id THEN
      RAISE EXCEPTION 'APPROVALS_MAKER_RECUSED';
    END IF;
    IF NOT institution.approvals_is_eligible(p_stage_instance, v_effective) THEN
      RAISE EXCEPTION 'APPROVALS_NOT_ELIGIBLE';
    END IF;
    IF p_action IN ('reject') AND (p_comment IS NULL OR length(trim(p_comment)) = 0) THEN
      RAISE EXCEPTION 'APPROVALS_COMMENT_REQUIRED';
    END IF;
  END IF;

  INSERT INTO approval_action (stage_instance_id, actor_id, acting_as, action, comment, checklist_state)
  VALUES (p_stage_instance, p_actor, p_acting_as, p_action, p_comment, p_checklist);

  v_result := institution.approvals_evaluate_stage(p_stage_instance);
  RETURN v_result;
END $$;

CREATE OR REPLACE FUNCTION institution.approvals_evaluate_stage(p_stage_instance uuid)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE
  v record; c record;
  n_approve int; n_reject int; n_eligible int; needed int;
  chair_vote text; v_new text := 'active'; v_checked int; v_required int;
BEGIN
  SELECT si.id, si.seq, si.instance_id, sd.stage_type, sd.committee_id,
         sd.checklist AS checklist_def, i.institution_id, i.entity_type, i.entity_id
    INTO v
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  JOIN approval_instance i ON i.id = si.instance_id
  WHERE si.id = p_stage_instance FOR UPDATE OF si;

  SELECT count(*) FILTER (WHERE action = 'approve'),
         count(*) FILTER (WHERE action = 'reject')
    INTO n_approve, n_reject
  FROM approval_action WHERE stage_instance_id = p_stage_instance;

  IF v.stage_type = 'single' THEN
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 1 THEN v_new := 'approved'; END IF;

  ELSIF v.stage_type = 'dual' THEN
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 2 THEN v_new := 'approved'; END IF;

  ELSIF v.stage_type = 'committee' THEN
    SELECT * INTO c FROM approval_committee WHERE id = v.committee_id;
    SELECT count(*) INTO n_eligible FROM committee_member cm
    WHERE cm.committee_id = v.committee_id AND cm.is_voting
      AND cm.valid_from <= current_date
      AND (cm.valid_to IS NULL OR cm.valid_to >= current_date);

    needed := CASE c.quorum_type
      WHEN 'count'     THEN c.quorum_value::int
      WHEN 'fraction'  THEN GREATEST(1, ceil(n_eligible * c.quorum_value)::int)
      WHEN 'unanimous' THEN n_eligible END;

    IF n_approve >= needed THEN v_new := 'approved';
    ELSIF n_reject >= needed OR (c.quorum_type = 'unanimous' AND n_reject > 0) THEN
      v_new := 'rejected';
    ELSIF n_approve + n_reject >= n_eligible AND n_eligible > 0 THEN
      -- everyone voted, no quorum either way → tie-break
      IF c.tie_break = 'chair' THEN
        SELECT a.action INTO chair_vote
        FROM approval_action a
        JOIN committee_member cm ON cm.member_id = COALESCE(a.acting_as, a.actor_id)
        WHERE a.stage_instance_id = p_stage_instance
          AND cm.committee_id = v.committee_id AND cm.role = 'chair'
          AND a.action IN ('approve','reject')
        ORDER BY a.created_at DESC LIMIT 1;
        v_new := CASE WHEN chair_vote = 'approve' THEN 'approved' ELSE 'rejected' END;
      ELSIF c.tie_break = 'reject' THEN v_new := 'rejected';
      ELSE v_new := 'rejected';  -- 'escalate' handled by SLA/escalation path in v2
      END IF;
    END IF;

  ELSIF v.stage_type = 'checklist' THEN
    SELECT count(*) INTO v_required
    FROM jsonb_array_elements(v.checklist_def) e WHERE (e->>'required')::boolean;
    SELECT count(DISTINCT e.key) INTO v_checked
    FROM approval_action a,
         LATERAL jsonb_each_text(COALESCE(a.checklist_state,'{}'::jsonb)) e(key, val)
    WHERE a.stage_instance_id = p_stage_instance AND e.val = 'true';
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 1 AND v_checked >= v_required THEN v_new := 'approved'; END IF;

  ELSIF v.stage_type = 'external_hold' THEN
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 1 THEN v_new := 'approved'; END IF;
  END IF;

  IF v_new = 'active' THEN RETURN 'active'; END IF;

  UPDATE approval_stage_instance
     SET status = v_new, resolved_at = now() WHERE id = p_stage_instance;

  INSERT INTO approval_outbox (institution_id, event, payload)
  VALUES (v.institution_id, 'approval.stage.completed',
          jsonb_build_object('instance_id', v.instance_id, 'seq', v.seq, 'outcome', v_new));

  IF v_new = 'rejected' THEN
    UPDATE approval_instance SET status = 'rejected', resolved_at = now()
     WHERE id = v.instance_id;
    INSERT INTO approval_outbox (institution_id, event, payload)
    VALUES (v.institution_id, 'approval.rejected',
            jsonb_build_object('instance_id', v.instance_id,
                               'entity_type', v.entity_type, 'entity_id', v.entity_id));
    RETURN 'rejected';
  END IF;

  -- advance or complete
  PERFORM institution.approvals_advance(v.instance_id, v.seq);
  RETURN 'approved';
END $$;

CREATE OR REPLACE FUNCTION institution.approvals_advance(p_instance uuid, p_done_seq int)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v_next record; v record;
BEGIN
  SELECT i.*, i.institution_id AS iid INTO v FROM approval_instance i WHERE i.id = p_instance FOR UPDATE;

  SELECT si.*, sd.sla_hours INTO v_next
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  WHERE si.instance_id = p_instance AND si.seq = p_done_seq + 1;

  IF FOUND THEN
    UPDATE approval_stage_instance
       SET status = 'active', started_at = now(),
           due_at = CASE WHEN v_next.sla_hours IS NOT NULL
                         THEN now() + make_interval(hours => v_next.sla_hours) END
     WHERE id = v_next.id;
    UPDATE approval_instance SET current_seq = p_done_seq + 1 WHERE id = p_instance;
  ELSE
    UPDATE approval_instance SET status = 'approved', resolved_at = now() WHERE id = p_instance;
    INSERT INTO approval_outbox (institution_id, event, payload)
    VALUES (v.iid, 'approval.completed',
            jsonb_build_object('instance_id', p_instance,
                               'entity_type', v.entity_type, 'entity_id', v.entity_id));
  END IF;
END $$;

CREATE OR REPLACE FUNCTION institution.approvals_withdraw(
  p_instance uuid, p_actor uuid, p_reason text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v record;
BEGIN
  SELECT * INTO v FROM approval_instance WHERE id = p_instance FOR UPDATE;
  IF NOT FOUND OR v.status <> 'in_progress' THEN
    RAISE EXCEPTION 'APPROVALS_NOT_WITHDRAWABLE';
  END IF;
  UPDATE approval_instance
     SET status = 'withdrawn', withdraw_reason = p_reason, resolved_at = now()
   WHERE id = p_instance;
  UPDATE approval_stage_instance SET status = 'skipped', resolved_at = now()
   WHERE instance_id = p_instance AND status IN ('active','pending');
  INSERT INTO approval_outbox (institution_id, event, payload)
  VALUES (v.institution_id, 'approval.withdrawn',
          jsonb_build_object('instance_id', p_instance, 'reason', p_reason,
                             'actor', p_actor));
END $$;

CREATE OR REPLACE FUNCTION institution.approvals_expire_overdue()
RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT si.id AS stage_id, si.instance_id, sd.on_sla_breach,
           sd.escalate_to_template_id, i.institution_id, i.entity_type, i.entity_id
    FROM approval_stage_instance si
    JOIN approval_stage_def sd ON sd.id = si.stage_def_id
    JOIN approval_instance i ON i.id = si.instance_id
    WHERE si.status = 'active' AND si.due_at IS NOT NULL AND si.due_at < now()
      AND i.status = 'in_progress'
    FOR UPDATE OF si SKIP LOCKED
  LOOP
    n := n + 1;
    IF r.on_sla_breach = 'notify' THEN
      -- re-notify at most hourly: bump due_at, emit event
      UPDATE approval_stage_instance SET due_at = now() + interval '1 hour'
       WHERE id = r.stage_id;
      INSERT INTO approval_outbox (institution_id, event, payload)
      VALUES (r.institution_id, 'approval.sla.breached',
              jsonb_build_object('instance_id', r.instance_id, 'stage_id', r.stage_id));
    ELSIF r.on_sla_breach = 'auto_reject' THEN
      UPDATE approval_stage_instance SET status = 'expired', resolved_at = now()
       WHERE id = r.stage_id;
      UPDATE approval_instance SET status = 'expired', resolved_at = now()
       WHERE id = r.instance_id;
      INSERT INTO approval_outbox (institution_id, event, payload)
      VALUES (r.institution_id, 'approval.expired',
              jsonb_build_object('instance_id', r.instance_id));
    ELSIF r.on_sla_breach = 'escalate' THEN
      UPDATE approval_stage_instance SET status = 'expired', resolved_at = now()
       WHERE id = r.stage_id;
      UPDATE approval_instance SET status = 'escalated', resolved_at = now()
       WHERE id = r.instance_id;
      INSERT INTO approval_outbox (institution_id, event, payload)
      VALUES (r.institution_id, 'approval.escalated',
              jsonb_build_object('instance_id', r.instance_id,
                                 'escalate_to_template_id', r.escalate_to_template_id,
                                 'entity_type', r.entity_type, 'entity_id', r.entity_id));
      -- portal-api worker re-routes onto escalation template via approvals_route
    END IF;
  END LOOP;
  RETURN n;
END $$;

-- pg_cron (run as superuser once):
-- SELECT cron.schedule('approvals-sla', '*/15 * * * *',
--   $$SELECT institution.approvals_expire_overdue()$$);

-- ---------- 6. Seed: default maker-checker template ----------
CREATE OR REPLACE FUNCTION institution.approvals_seed_defaults(
  p_institution_id uuid, p_admin uuid
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v_tpl uuid;
BEGIN
  INSERT INTO approval_template (institution_id, name, entity_type, status, created_by, approved_by)
  VALUES (p_institution_id, 'Default dual control', 'bid', 'active', p_admin, p_admin)
  ON CONFLICT (institution_id, name, version) DO NOTHING
  RETURNING id INTO v_tpl;
  IF v_tpl IS NULL THEN RETURN; END IF;

  INSERT INTO approval_stage_def (template_id, seq, name, stage_type, approver_role, sla_hours)
  VALUES (v_tpl, 1, 'Checker approval', 'dual', 'checker', 48);

  -- mandatory catch-all DoA rule
  INSERT INTO doa_rule (institution_id, entity_type, priority, conditions, template_id, created_by)
  VALUES (p_institution_id, 'bid', 9999, '{}'::jsonb, v_tpl, p_admin)
  ON CONFLICT DO NOTHING;
END $$;

COMMIT;
