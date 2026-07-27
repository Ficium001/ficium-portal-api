-- =============================================================================
-- 009_doc_templates.sql — inst:doctemplates
-- Institution-authored .docx templates (loan agreement, facility letter, etc.),
-- maker-checker version publish, and merge-field generation against a deal
-- (loan_pipeline) snapshot. Storage: Supabase Storage, bucket
-- `institution-docs`, prefix `doc-templates/`.
-- Gate: institution-level licensing via require_module("inst:doctemplates")
-- (institution.institution.modules), same mechanism as the pipeline module.
-- =============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS institution.doc_template (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id   uuid NOT NULL,
  code             text NOT NULL,
  name             text NOT NULL,
  description      text,
  doc_category     text NOT NULL DEFAULT 'other' CHECK (doc_category IN
                   ('loan_agreement','facility_letter','legal_agreement','terms_conditions','other')),
  product_id       uuid,
  product_code     text,
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN
                   ('draft','pending_approval','active','retired')),
  current_version  int  NOT NULL DEFAULT 0,
  created_by       uuid NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (institution_id, code)
);
CREATE INDEX IF NOT EXISTS idx_dt_inst ON institution.doc_template(institution_id);

CREATE TABLE IF NOT EXISTS institution.doc_template_version (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id   uuid NOT NULL,
  template_id      uuid NOT NULL REFERENCES institution.doc_template(id),
  version_no       int  NOT NULL,
  file_name        text NOT NULL,
  storage_path     text NOT NULL,
  file_size_bytes  bigint,
  checksum_sha256  text,
  merge_field_map  jsonb NOT NULL DEFAULT '{}',
  change_note      text,
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN
                   ('draft','pending_approval','published','retired','rejected')),
  created_by       uuid NOT NULL,
  approved_by      uuid,
  approved_at      timestamptz,
  rejection_note   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (template_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_dtv_tmpl ON institution.doc_template_version(template_id);

CREATE TABLE IF NOT EXISTS institution.doc_generation (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id       uuid NOT NULL,
  template_id          uuid NOT NULL REFERENCES institution.doc_template(id),
  template_version_id  uuid NOT NULL REFERENCES institution.doc_template_version(id),
  entity_type          text NOT NULL DEFAULT 'loan_pipeline',
  entity_id            uuid NOT NULL,
  stage_instance_id    uuid,
  data_snapshot        jsonb NOT NULL DEFAULT '{}',
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN
                       ('pending','generating','generated','failed')),
  error                text,
  output_docx_path     text,
  output_pdf_path      text,
  esign_envelope_id    uuid,
  generated_by         uuid NOT NULL,
  generated_at         timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dg_inst ON institution.doc_generation(institution_id);
CREATE INDEX IF NOT EXISTS idx_dg_entity ON institution.doc_generation(entity_type, entity_id);

-- RLS
DO $rls$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['doc_template','doc_template_version','doc_generation'] LOOP
    EXECUTE format('ALTER TABLE institution.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE institution.%I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $rls$;

DROP POLICY IF EXISTS dt_tenant ON institution.doc_template;
CREATE POLICY dt_tenant ON institution.doc_template
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));

DROP POLICY IF EXISTS dtv_tenant ON institution.doc_template_version;
CREATE POLICY dtv_tenant ON institution.doc_template_version
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));

DROP POLICY IF EXISTS dg_tenant ON institution.doc_generation;
CREATE POLICY dg_tenant ON institution.doc_generation
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));

COMMIT;
