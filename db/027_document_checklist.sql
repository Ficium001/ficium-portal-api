-- =============================================================================
-- 027_document_checklist.sql — the document checklist for deals.
--
-- WHY
--   Documents lived in five unconnected places, and a stage's "requires
--   documents" flag was only a label: nothing could be uploaded against a
--   stage and nothing was checked before it advanced (stage documents were a
--   hard-coded empty list).
--
-- WHAT
--   1. institution.doc_requirement — per workflow (pipeline template): which
--      document, at which stage, provided by whom, mandatory or not.
--   2. marketplace.deal_document — one row per requirement per deal, with its
--      file and status: required -> received -> verified, or rejected (re-upload)
--      or waived (with reason). marketplace.deal_document_event keeps every
--      step (who, when, file, note) for audit.
--   3. ensure_deal_documents(deal): materialises the checklist (idempotent, so
--      existing deals and later requirement changes are picked up).
--   4. deal_document_act(): every transition, with the rules —
--        verify needs a different person than the uploader (four eyes),
--        reject and waive need a note, nothing changes on a closed deal,
--        a verified document can't be replaced.
--   5. stage_documents_outstanding(stage): mandatory documents of that stage
--      not yet verified or waived. portal-api refuses to advance the stage
--      while it is > 0.
--   6. Starter checklist for templates whose stages already said "requires
--      documents" (credit_docs, legal_review). Editable in Setup.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS institution.doc_requirement (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    institution_id uuid NOT NULL REFERENCES institution.institution(id) ON DELETE CASCADE,
    template_id    uuid NOT NULL REFERENCES institution.pipeline_template(id) ON DELETE CASCADE,
    stage_def_id   uuid REFERENCES institution.pipeline_stage_def(id) ON DELETE SET NULL,
    name           text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    description    text CHECK (description IS NULL OR char_length(description) <= 500),
    provided_by    text NOT NULL DEFAULT 'borrower' CHECK (provided_by IN ('borrower', 'bank', 'third_party')),
    mandatory      boolean NOT NULL DEFAULT true,
    position       integer NOT NULL DEFAULT 0,
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_doc_requirement_template ON institution.doc_requirement (template_id);

CREATE TABLE IF NOT EXISTS marketplace.deal_document (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id    uuid NOT NULL REFERENCES marketplace.loan_pipeline(id) ON DELETE CASCADE,
    requirement_id uuid NOT NULL REFERENCES institution.doc_requirement(id) ON DELETE RESTRICT,
    institution_id uuid NOT NULL,
    status         text NOT NULL DEFAULT 'required'
                   CHECK (status IN ('required', 'received', 'verified', 'rejected', 'waived')),
    storage_path   text,
    file_name      text,
    mime_type      text,
    file_size      bigint,
    uploaded_by    uuid,
    uploaded_at    timestamptz,
    reviewed_by    uuid,
    reviewed_at    timestamptz,
    review_note    text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_deal_document UNIQUE (pipeline_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS marketplace.deal_document_event (
    id          bigserial PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES marketplace.deal_document(id) ON DELETE CASCADE,
    action      text NOT NULL,
    actor_id    uuid,
    note        text,
    file_name   text,
    storage_path text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_deal_document_event_doc ON marketplace.deal_document_event (document_id);

-- Tenant read; all writes go through the functions below as postgres.
ALTER TABLE institution.doc_requirement   ENABLE ROW LEVEL SECURITY;
ALTER TABLE institution.doc_requirement   FORCE ROW LEVEL SECURITY;
ALTER TABLE marketplace.deal_document     ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace.deal_document     FORCE ROW LEVEL SECURITY;
ALTER TABLE marketplace.deal_document_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace.deal_document_event FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS doc_requirement_tenant_read ON institution.doc_requirement;
CREATE POLICY doc_requirement_tenant_read ON institution.doc_requirement FOR SELECT
    USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS deal_document_tenant_read ON marketplace.deal_document;
CREATE POLICY deal_document_tenant_read ON marketplace.deal_document FOR SELECT
    USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS deal_document_event_tenant_read ON marketplace.deal_document_event;
CREATE POLICY deal_document_event_tenant_read ON marketplace.deal_document_event FOR SELECT
    USING (EXISTS (SELECT 1 FROM marketplace.deal_document d
                    WHERE d.id = document_id
                      AND d.institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)));

GRANT SELECT ON institution.doc_requirement, marketplace.deal_document, marketplace.deal_document_event TO authenticated;

-- 3 ── materialise ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION marketplace.ensure_deal_documents(p_pipeline_id uuid)
RETURNS integer
LANGUAGE plpgsql
SET search_path TO 'marketplace', 'institution', 'pg_temp'
AS $fn$
DECLARE n integer;
BEGIN
    INSERT INTO marketplace.deal_document (pipeline_id, requirement_id, institution_id)
    SELECT lp.id, r.id, lp.institution_id
      FROM marketplace.loan_pipeline lp
      JOIN institution.doc_requirement r
        ON r.template_id = lp.template_id AND r.institution_id = lp.institution_id AND r.is_active
     WHERE lp.id = p_pipeline_id
    ON CONFLICT (pipeline_id, requirement_id) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $fn$;

-- 4 ── transitions ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION marketplace.deal_document_act(
    p_document_id    uuid,
    p_institution_id uuid,
    p_action         text,
    p_actor          uuid,
    p_note           text DEFAULT NULL,
    p_storage_path   text DEFAULT NULL,
    p_file_name      text DEFAULT NULL,
    p_mime_type      text DEFAULT NULL,
    p_file_size      bigint DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'marketplace', 'institution', 'pg_temp'
AS $fn$
DECLARE
    d   marketplace.deal_document%ROWTYPE;
    lp  marketplace.loan_pipeline%ROWTYPE;
    v_note text := NULLIF(btrim(COALESCE(p_note, '')), '');
BEGIN
    SELECT * INTO d FROM marketplace.deal_document
     WHERE id = p_document_id AND institution_id = p_institution_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'DOC_NOT_FOUND'; END IF;
    SELECT * INTO lp FROM marketplace.loan_pipeline WHERE id = d.pipeline_id;
    IF lp.status <> 'active' THEN RAISE EXCEPTION 'DOC_DEAL_CLOSED'; END IF;

    IF p_action = 'receive' THEN
        IF d.status = 'verified' THEN RAISE EXCEPTION 'DOC_ALREADY_VERIFIED'; END IF;
        IF p_storage_path IS NULL OR p_file_name IS NULL THEN RAISE EXCEPTION 'DOC_FILE_REQUIRED'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'received', storage_path = p_storage_path, file_name = p_file_name,
               mime_type = p_mime_type, file_size = p_file_size,
               uploaded_by = p_actor, uploaded_at = now(),
               reviewed_by = NULL, reviewed_at = NULL, review_note = NULL, updated_at = now()
         WHERE id = d.id;

    ELSIF p_action = 'verify' THEN
        IF d.status <> 'received' THEN RAISE EXCEPTION 'DOC_NOT_RECEIVED'; END IF;
        IF d.uploaded_by = p_actor THEN RAISE EXCEPTION 'DOC_FOUR_EYES'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'verified', reviewed_by = p_actor, reviewed_at = now(),
               review_note = v_note, updated_at = now()
         WHERE id = d.id;

    ELSIF p_action = 'reject' THEN
        IF d.status <> 'received' THEN RAISE EXCEPTION 'DOC_NOT_RECEIVED'; END IF;
        IF v_note IS NULL THEN RAISE EXCEPTION 'DOC_NOTE_REQUIRED'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'rejected', reviewed_by = p_actor, reviewed_at = now(),
               review_note = v_note, updated_at = now()
         WHERE id = d.id;

    ELSIF p_action = 'waive' THEN
        IF d.status IN ('verified', 'waived') THEN RAISE EXCEPTION 'DOC_NOT_WAIVABLE'; END IF;
        IF v_note IS NULL THEN RAISE EXCEPTION 'DOC_NOTE_REQUIRED'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'waived', reviewed_by = p_actor, reviewed_at = now(),
               review_note = v_note, updated_at = now()
         WHERE id = d.id;

    ELSE
        RAISE EXCEPTION 'DOC_ACTION_INVALID';
    END IF;

    INSERT INTO marketplace.deal_document_event (document_id, action, actor_id, note, file_name, storage_path)
    VALUES (d.id, p_action, p_actor, v_note,
            CASE WHEN p_action = 'receive' THEN p_file_name END,
            CASE WHEN p_action = 'receive' THEN p_storage_path END);

    SELECT * INTO d FROM marketplace.deal_document WHERE id = d.id;
    RETURN to_jsonb(d);
END $fn$;

-- 5 ── gate ────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION marketplace.stage_documents_outstanding(p_stage_instance_id uuid)
RETURNS integer
LANGUAGE sql STABLE
SET search_path TO 'marketplace', 'institution', 'pg_temp'
AS $fn$
    SELECT count(*)::int
      FROM marketplace.pipeline_stage_instance psi
      JOIN marketplace.deal_document dd ON dd.pipeline_id = psi.pipeline_id
      JOIN institution.doc_requirement r ON r.id = dd.requirement_id
     WHERE psi.id = p_stage_instance_id
       AND r.stage_def_id = psi.stage_def_id
       AND r.mandatory
       AND dd.status NOT IN ('verified', 'waived');
$fn$;

REVOKE ALL ON FUNCTION marketplace.ensure_deal_documents(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION marketplace.deal_document_act(uuid, uuid, text, uuid, text, text, text, text, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION marketplace.stage_documents_outstanding(uuid) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.ensure_deal_documents(uuid) FROM anon, authenticated';
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.deal_document_act(uuid, uuid, text, uuid, text, text, text, text, bigint) FROM anon, authenticated';
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.stage_documents_outstanding(uuid) FROM anon, authenticated';
  END IF;
END $$;

-- 6 ── starter checklist ───────────────────────────────────────────────────────
INSERT INTO institution.doc_requirement (institution_id, template_id, stage_def_id, name, description, provided_by, mandatory, position)
SELECT sd.institution_id, sd.template_id, sd.id, x.name, x.description, x.provided_by, x.mandatory, x.position
  FROM institution.pipeline_stage_def sd
  CROSS JOIN LATERAL (VALUES
      ('Proof of income',          'Last 3 payslips, or audited accounts for self-employed borrowers.', 'borrower', true, 1),
      ('Bank statements',          'Last 3 months, all accounts used for salary or business income.',  'borrower', true, 2),
      ('Proof of address',         'Utility bill or bank letter, less than 3 months old.',               'borrower', true, 3)
  ) AS x(name, description, provided_by, mandatory, position)
 WHERE sd.stage_key = 'credit_docs'
   AND NOT EXISTS (SELECT 1 FROM institution.doc_requirement r WHERE r.stage_def_id = sd.id)
UNION ALL
SELECT sd.institution_id, sd.template_id, sd.id, 'Security documents',
       'Charge, guarantee or pledge documents for the facility, as applicable.', 'bank', false, 10
  FROM institution.pipeline_stage_def sd
 WHERE sd.stage_key = 'legal_review'
   -- lending workflows only (they have a credit documentation stage)
   AND EXISTS (SELECT 1 FROM institution.pipeline_stage_def c
                WHERE c.template_id = sd.template_id AND c.stage_key = 'credit_docs')
   AND NOT EXISTS (SELECT 1 FROM institution.doc_requirement r WHERE r.stage_def_id = sd.id);

COMMIT;
