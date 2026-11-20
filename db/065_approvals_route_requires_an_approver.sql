-- =============================================================================
-- 065 — An approval cannot start unless every step can actually be approved.
--
-- approvals_route created the instance and its steps with no check that anyone
-- could act on them. A step nobody is eligible for sat 'active' forever: the
-- deal showed "awaiting approval" and no one in the bank could move it. Seen in
-- the golden path (its MCB approver had no 'checker' role), and reachable in
-- production by any bank whose only checker is the person submitting.
--
-- Now, after creating the steps and in the same transaction, every step is
-- counted with the engine's own rules:
--   available = active members of the bank, other than the maker, for whom
--               approvals_is_eligible(step, member) is true (approvals_cast
--               refuses the maker: APPROVALS_MAKER_RECUSED)
--   needed    = what approvals_evaluate_stage needs to approve the step:
--               single, checklist, external_hold 1; dual 2; committee by its
--               quorum (count, fraction of voting members, or unanimous),
--               counting voting members as approvals_evaluate_stage does
-- If any step has available < needed, nothing is created and the call raises
-- APPROVALS_NO_APPROVER with the step, the role it needs and the numbers.
--
-- Also caught: a unanimous committee that includes the maker (the maker can
-- never vote, so unanimity is unreachable) and a committee with no voting
-- members (needed is at least 1).
--
-- Everything else in approvals_route is unchanged from the live definition.
-- Exercised against the live schema in a rolled-back transaction before being
-- applied (see the PR).
-- =============================================================================

CREATE OR REPLACE FUNCTION institution.approvals_route(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_entity_maker uuid, p_snapshot jsonb, p_template_id uuid, p_doa_rule_id uuid)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'institution', 'public'
AS $function$
DECLARE v_instance uuid; v_version int; v_stage record; v_first_due timestamptz;
        v_step record; v_voters int; v_needed int; v_available int; c record;
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

  -- 065: every step must be approvable by someone other than the maker.
  FOR v_step IN
    SELECT si.id, si.seq, sd.name, sd.stage_type, sd.committee_id, sd.approver_role
      FROM approval_stage_instance si
      JOIN approval_stage_def sd ON sd.id = si.stage_def_id
     WHERE si.instance_id = v_instance
     ORDER BY si.seq
  LOOP
    SELECT count(*) INTO v_available
      FROM member m
     WHERE m.institution_id = p_institution_id AND m.active
       AND m.auth_user_id IS DISTINCT FROM p_entity_maker
       AND institution.approvals_is_eligible(v_step.id, m.auth_user_id);

    IF v_step.stage_type = 'dual' THEN
      v_needed := 2;
    ELSIF v_step.stage_type = 'committee' THEN
      SELECT * INTO c FROM approval_committee WHERE id = v_step.committee_id;
      SELECT count(*) INTO v_voters FROM committee_member cm
       WHERE cm.committee_id = v_step.committee_id AND cm.is_voting
         AND cm.valid_from <= current_date
         AND (cm.valid_to IS NULL OR cm.valid_to >= current_date);
      v_needed := CASE c.quorum_type
        WHEN 'count'     THEN c.quorum_value::int
        WHEN 'fraction'  THEN GREATEST(1, ceil(v_voters * c.quorum_value)::int)
        WHEN 'unanimous' THEN v_voters END;
      v_needed := GREATEST(COALESCE(v_needed, 1), 1);
    ELSE
      v_needed := 1;
    END IF;

    IF v_available < v_needed THEN
      RAISE EXCEPTION 'APPROVALS_NO_APPROVER'
        USING DETAIL = format(
          'Step %s "%s" needs %s approver(s) other than the submitter%s; %s can act.',
          v_step.seq, v_step.name, v_needed,
          CASE WHEN v_step.stage_type = 'committee' THEN ' on its committee'
               WHEN v_step.approver_role IS NOT NULL
               THEN format(' with the %s role', v_step.approver_role)
               ELSE '' END,
          v_available);
    END IF;
  END LOOP;

  INSERT INTO approval_outbox (institution_id, event, payload)
  VALUES (p_institution_id, 'approval.requested',
          jsonb_build_object('instance_id', v_instance, 'entity_type', p_entity_type,
                             'entity_id', p_entity_id));
  RETURN v_instance;
END $function$;
