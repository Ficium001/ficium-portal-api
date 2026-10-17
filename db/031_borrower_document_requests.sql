-- =============================================================================
-- 031_borrower_document_requests.sql — contract 1.5.0, institution side.
--
-- A bank requests documents for a deal the borrower accepted; the borrower
-- shares a vault copy or uploads a new one, straight to storage.
--
-- 1. doc_requirement.document_type / max_age_days: what kind of document and
--    how recent. Defaults set on the seeded checklist (payslips, statements,
--    proof of address: 90 days).
-- 2. deal_document.uploaded_via ('bank' | 'borrower'), source ('vault' | 'new'),
--    document_date: so bank staff see where a file came from and what it covers.
-- 3. integration.document_upload: one row per one-time upload slot.
-- 4. The checklist is created WITH the deal (it used to appear only once staff
--    opened the deal, so a borrower would see nothing until then).
-- 5. deal.documents_updated is published (outbox) whenever the checklist of a
--    Phase 2 deal changes. Phase 2 = the bid was accepted through the
--    integration (acceptance_log 200). Also on that acceptance itself, since the
--    log row can be written after the deal's documents.
-- =============================================================================

BEGIN;

-- 1 ───────────────────────────────────────────────────────────────────────────
ALTER TABLE institution.doc_requirement
    ADD COLUMN IF NOT EXISTS document_type text,
    ADD COLUMN IF NOT EXISTS max_age_days  integer;
ALTER TABLE institution.doc_requirement DROP CONSTRAINT IF EXISTS ck_doc_requirement_type;
ALTER TABLE institution.doc_requirement ADD CONSTRAINT ck_doc_requirement_type CHECK (
    document_type IS NULL OR document_type IN (
      'payslip','employment_letter','tax_return','bank_statement','loan_statement','credit_card_statement',
      'title_deed','valuation_report','land_registry_extract','brn_certificate','audited_accounts',
      'insurance_policy','nic','passport','birth_certificate','driving_licence','other'));
ALTER TABLE institution.doc_requirement DROP CONSTRAINT IF EXISTS ck_doc_requirement_max_age;
ALTER TABLE institution.doc_requirement ADD CONSTRAINT ck_doc_requirement_max_age
    CHECK (max_age_days IS NULL OR max_age_days BETWEEN 1 AND 3650);

UPDATE institution.doc_requirement SET document_type = 'payslip', max_age_days = 90
 WHERE name = 'Proof of income' AND document_type IS NULL;
UPDATE institution.doc_requirement SET document_type = 'bank_statement', max_age_days = 90
 WHERE name = 'Bank statements' AND document_type IS NULL;
UPDATE institution.doc_requirement SET document_type = 'other', max_age_days = 90
 WHERE name = 'Proof of address' AND document_type IS NULL;

-- 2 ───────────────────────────────────────────────────────────────────────────
ALTER TABLE marketplace.deal_document
    ADD COLUMN IF NOT EXISTS uploaded_via  text NOT NULL DEFAULT 'bank',
    ADD COLUMN IF NOT EXISTS source        text,
    ADD COLUMN IF NOT EXISTS document_date date;
ALTER TABLE marketplace.deal_document DROP CONSTRAINT IF EXISTS ck_deal_document_via;
ALTER TABLE marketplace.deal_document ADD CONSTRAINT ck_deal_document_via
    CHECK (uploaded_via IN ('bank','borrower') AND (source IS NULL OR source IN ('vault','new')));

-- 3 ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS integration.document_upload (
    upload_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id       uuid NOT NULL REFERENCES marketplace.loan_pipeline(id),
    deal_document_id  uuid NOT NULL REFERENCES marketplace.deal_document(id),
    requirement_id    uuid NOT NULL,
    institution_id    uuid NOT NULL,
    storage_path      text NOT NULL,
    file_name         text NOT NULL,
    mime_type         text NOT NULL,
    size_bytes        integer NOT NULL,
    source            text NOT NULL CHECK (source IN ('vault','new')),
    document_date     date,
    status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open','completed')),
    sha256            text,
    expires_at        timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz
);
CREATE INDEX IF NOT EXISTS ix_document_upload_doc ON integration.document_upload (deal_document_id);
ALTER TABLE integration.document_upload ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.document_upload FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON integration.document_upload FROM anon, authenticated';
  END IF;
END $$;

-- 5 (functions first; the triggers below use them) ─────────────────────────────
CREATE OR REPLACE FUNCTION integration.is_phase2(p_pipeline_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, integration, marketplace AS $$
  SELECT EXISTS (
    SELECT 1 FROM marketplace.loan_pipeline lp
      JOIN integration.acceptance_log a ON a.bid_id = lp.bid_id AND a.status_code = 200
     WHERE lp.id = p_pipeline_id);
$$;

CREATE OR REPLACE FUNCTION integration.build_deal_documents_payload(p_pipeline_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
  SELECT jsonb_build_object(
    'pipeline_id', lp.id, 'request_id', lp.request_id, 'bid_id', lp.bid_id, 'updated_at', now(),
    'documents', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
          'requirement_id', r.id, 'name', r.name, 'description', r.description,
          'provided_by', r.provided_by, 'mandatory', r.mandatory, 'stage_label', sd.label,
          'status', dd.status,
          -- A review note is written for staff; only rejection/waiver reasons are the borrower's business.
          'review_note', CASE WHEN dd.status IN ('rejected','waived') THEN dd.review_note END,
          'document_type', r.document_type, 'max_age_days', r.max_age_days)
        ORDER BY r.position, r.name)
        FROM marketplace.deal_document dd
        JOIN institution.doc_requirement r ON r.id = dd.requirement_id
        LEFT JOIN institution.pipeline_stage_def sd ON sd.id = r.stage_def_id
       WHERE dd.pipeline_id = lp.id), '[]'::jsonb))
  FROM marketplace.loan_pipeline lp
  WHERE lp.id = p_pipeline_id AND lp.bid_id IS NOT NULL;
$$;

CREATE OR REPLACE FUNCTION integration.publish_deal_documents(p_pipeline_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE d jsonb; last_docs jsonb;
BEGIN
  IF NOT integration.is_phase2(p_pipeline_id) THEN RETURN; END IF;
  d := integration.build_deal_documents_payload(p_pipeline_id);
  IF d IS NULL THEN RETURN; END IF;
  SELECT o.envelope -> 'data' -> 'documents' INTO last_docs FROM integration.outbox o
   WHERE o.aggregate_id = p_pipeline_id::text AND o.type = 'deal.documents_updated'
   ORDER BY o.sequence DESC LIMIT 1;
  IF last_docs = d -> 'documents' THEN RETURN; END IF;  -- nothing the borrower can see changed
  PERFORM integration.enqueue('deal.documents_updated', p_pipeline_id::text, d, 'institution');
END $$;

CREATE OR REPLACE FUNCTION integration.trg_deal_documents_emit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE pid uuid;
BEGIN
  -- Separate branches: one expression naming both NEW.pipeline_id and NEW.bid_id
  -- fails on whichever table lacks the field.
  IF TG_TABLE_NAME = 'deal_document' THEN
    pid := NEW.pipeline_id;
  ELSE
    SELECT lp.id INTO pid FROM marketplace.loan_pipeline lp WHERE lp.bid_id = NEW.bid_id;
  END IF;
  IF pid IS NULL THEN RETURN NULL; END IF;
  BEGIN PERFORM integration.publish_deal_documents(pid);
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO integration.emit_error (aggregate_id, stage, error)
    VALUES (pid::text, TG_TABLE_NAME || ' ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION integration.trg_requirement_emit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE pid uuid;
BEGIN
  FOR pid IN SELECT DISTINCT dd.pipeline_id FROM marketplace.deal_document dd
               JOIN marketplace.loan_pipeline lp ON lp.id = dd.pipeline_id
              WHERE dd.requirement_id = NEW.id AND lp.status = 'active' LOOP
    BEGIN PERFORM integration.publish_deal_documents(pid);
    EXCEPTION WHEN OTHERS THEN
      INSERT INTO integration.emit_error (aggregate_id, stage, error)
      VALUES (pid::text, 'doc_requirement ' || TG_OP, left(SQLERRM, 500));
    END;
  END LOOP;
  RETURN NULL;
END $$;

-- 4: checklist with the deal ────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION marketplace.trg_pipeline_documents() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, marketplace AS $$
BEGIN
  PERFORM marketplace.ensure_deal_documents(NEW.id);
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS pipeline_documents ON marketplace.loan_pipeline;
CREATE CONSTRAINT TRIGGER pipeline_documents AFTER INSERT ON marketplace.loan_pipeline
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION marketplace.trg_pipeline_documents();

DROP TRIGGER IF EXISTS integration_deal_document_events ON marketplace.deal_document;
CREATE CONSTRAINT TRIGGER integration_deal_document_events AFTER INSERT OR UPDATE ON marketplace.deal_document
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_deal_documents_emit();

DROP TRIGGER IF EXISTS integration_acceptance_documents ON integration.acceptance_log;
CREATE CONSTRAINT TRIGGER integration_acceptance_documents AFTER INSERT OR UPDATE ON integration.acceptance_log
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.status_code = 200)
  EXECUTE FUNCTION integration.trg_deal_documents_emit();

DROP TRIGGER IF EXISTS integration_requirement_events ON institution.doc_requirement;
CREATE CONSTRAINT TRIGGER integration_requirement_events AFTER UPDATE ON institution.doc_requirement
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_requirement_emit();

REVOKE ALL ON FUNCTION integration.is_phase2(uuid), integration.build_deal_documents_payload(uuid),
  integration.publish_deal_documents(uuid), integration.trg_deal_documents_emit(),
  integration.trg_requirement_emit(), marketplace.trg_pipeline_documents() FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION integration.is_phase2(uuid), integration.build_deal_documents_payload(uuid),
      integration.publish_deal_documents(uuid), integration.trg_deal_documents_emit(),
      integration.trg_requirement_emit(), marketplace.trg_pipeline_documents() FROM anon, authenticated';
  END IF;
END $$;

COMMIT;
