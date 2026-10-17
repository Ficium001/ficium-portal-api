-- =============================================================================
-- 033_deal_progress_events.sql — contract 1.7.0. The borrower's loan tracker
-- runs on events: deal.progress_updated, full state, whenever a Phase 2 deal
-- (accepted through the integration) changes in a way the borrower can see.
-- Replaces the tracker's direct call to GET /public/requests/{id}/pipeline.
-- Only borrower-visible stages, with their borrower-facing labels.
-- =============================================================================
BEGIN;

CREATE OR REPLACE FUNCTION integration.build_deal_progress_payload(p_pipeline_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
  SELECT jsonb_build_object(
    'pipeline_id', lp.id, 'request_id', lp.request_id, 'bid_id', lp.bid_id,
    'status', lp.status::text,
    'deal', jsonb_build_object('amount', lp.deal_amount, 'rate', lp.deal_rate, 'term_months', lp.deal_term_months),
    'started_at', lp.started_at, 'completed_at', lp.completed_at, 'updated_at', now(),
    'stages', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
          'position', row_number, 'label', label, 'status', status,
          'started_at', started_at, 'completed_at', completed_at, 'due_at', sla_due_at) ORDER BY row_number)
        FROM (SELECT row_number() OVER (ORDER BY psi.position) AS row_number,
                     coalesce(nullif(psd.borrower_label, ''), psd.label) AS label,
                     psi.status::text AS status, psi.started_at, psi.completed_at, psi.sla_due_at
                FROM marketplace.pipeline_stage_instance psi
                JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
               WHERE psi.pipeline_id = lp.id AND psd.borrower_visible) v), '[]'::jsonb))
  FROM marketplace.loan_pipeline lp
  WHERE lp.id = p_pipeline_id AND lp.bid_id IS NOT NULL;
$$;

CREATE OR REPLACE FUNCTION integration.publish_deal_progress(p_pipeline_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE d jsonb; last_d jsonb;
BEGIN
  IF NOT integration.is_phase2(p_pipeline_id) THEN RETURN; END IF;
  d := integration.build_deal_progress_payload(p_pipeline_id);
  IF d IS NULL THEN RETURN; END IF;
  SELECT o.envelope -> 'data' INTO last_d FROM integration.outbox o
   WHERE o.aggregate_id = p_pipeline_id::text AND o.type = 'deal.progress_updated'
   ORDER BY o.sequence DESC LIMIT 1;
  IF last_d - 'updated_at' = d - 'updated_at' THEN RETURN; END IF;  -- nothing the borrower sees changed
  PERFORM integration.enqueue('deal.progress_updated', p_pipeline_id::text, d, 'institution');
END $$;

CREATE OR REPLACE FUNCTION integration.trg_deal_progress_emit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE pid uuid;
BEGIN
  -- Separate branches: each table has different columns (NEW.id / NEW.pipeline_id / NEW.bid_id).
  IF TG_TABLE_NAME = 'loan_pipeline' THEN
    pid := NEW.id;
  ELSIF TG_TABLE_NAME = 'pipeline_stage_instance' THEN
    pid := NEW.pipeline_id;
  ELSE
    SELECT lp.id INTO pid FROM marketplace.loan_pipeline lp WHERE lp.bid_id = NEW.bid_id;
  END IF;
  IF pid IS NULL THEN RETURN NULL; END IF;
  BEGIN PERFORM integration.publish_deal_progress(pid);
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO integration.emit_error (aggregate_id, stage, error)
    VALUES (pid::text, 'progress ' || TG_TABLE_NAME || ' ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS integration_deal_progress_pipeline ON marketplace.loan_pipeline;
CREATE CONSTRAINT TRIGGER integration_deal_progress_pipeline AFTER INSERT OR UPDATE ON marketplace.loan_pipeline
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_deal_progress_emit();
DROP TRIGGER IF EXISTS integration_deal_progress_stage ON marketplace.pipeline_stage_instance;
CREATE CONSTRAINT TRIGGER integration_deal_progress_stage AFTER INSERT OR UPDATE ON marketplace.pipeline_stage_instance
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_deal_progress_emit();
DROP TRIGGER IF EXISTS integration_acceptance_progress ON integration.acceptance_log;
CREATE CONSTRAINT TRIGGER integration_acceptance_progress AFTER INSERT OR UPDATE ON integration.acceptance_log
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.status_code = 200)
  EXECUTE FUNCTION integration.trg_deal_progress_emit();

REVOKE ALL ON FUNCTION integration.build_deal_progress_payload(uuid), integration.publish_deal_progress(uuid),
  integration.trg_deal_progress_emit() FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION integration.build_deal_progress_payload(uuid), integration.publish_deal_progress(uuid),
      integration.trg_deal_progress_emit() FROM anon, authenticated';
  END IF;
END $$;

COMMIT;
