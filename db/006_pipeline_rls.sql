-- =============================================================================
-- 006_pipeline_rls.sql
--
-- SECURITY FIX: four tenant-scoped tables were shipped with RLS DISABLED,
-- so with the API running as the `authenticated` role any authenticated
-- user from ANY institution could read/write another institution's loan
-- pipeline data. This migration enables + forces RLS and adds per-tenant
-- isolation policies matching the established pattern used on
-- marketplace.bid / marketplace.request (institution.current_member_ctx_v2()).
--
-- Tables fixed:
--   institution.pipeline_template          (institution_id)
--   institution.pipeline_stage_def         (institution_id)
--   marketplace.loan_pipeline              (institution_id)
--   marketplace.pipeline_stage_instance    (via pipeline_id -> loan_pipeline)
--
-- Idempotent: safe to re-run (drops policies before recreating).
-- =============================================================================

-- ── institution.pipeline_template ───────────────────────────────────────────
ALTER TABLE institution.pipeline_template ENABLE ROW LEVEL SECURITY;
ALTER TABLE institution.pipeline_template FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pipeline_template_tenant ON institution.pipeline_template;
CREATE POLICY pipeline_template_tenant ON institution.pipeline_template
    USING (
        institution_id = (
            SELECT ctx.institution_id
            FROM institution.current_member_ctx_v2() ctx
        )
    )
    WITH CHECK (
        institution_id = (
            SELECT ctx.institution_id
            FROM institution.current_member_ctx_v2() ctx
        )
    );

-- ── institution.pipeline_stage_def ──────────────────────────────────────────
ALTER TABLE institution.pipeline_stage_def ENABLE ROW LEVEL SECURITY;
ALTER TABLE institution.pipeline_stage_def FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pipeline_stage_def_tenant ON institution.pipeline_stage_def;
CREATE POLICY pipeline_stage_def_tenant ON institution.pipeline_stage_def
    USING (
        institution_id = (
            SELECT ctx.institution_id
            FROM institution.current_member_ctx_v2() ctx
        )
    )
    WITH CHECK (
        institution_id = (
            SELECT ctx.institution_id
            FROM institution.current_member_ctx_v2() ctx
        )
    );

-- ── marketplace.loan_pipeline ───────────────────────────────────────────────
ALTER TABLE marketplace.loan_pipeline ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace.loan_pipeline FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS loan_pipeline_tenant ON marketplace.loan_pipeline;
CREATE POLICY loan_pipeline_tenant ON marketplace.loan_pipeline
    USING (
        institution_id = (
            SELECT ctx.institution_id
            FROM institution.current_member_ctx_v2() ctx
        )
    )
    WITH CHECK (
        institution_id = (
            SELECT ctx.institution_id
            FROM institution.current_member_ctx_v2() ctx
        )
    );

-- ── marketplace.pipeline_stage_instance ─────────────────────────────────────
-- No institution_id column; scoped via its parent loan_pipeline.
ALTER TABLE marketplace.pipeline_stage_instance ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace.pipeline_stage_instance FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pipeline_stage_instance_tenant ON marketplace.pipeline_stage_instance;
CREATE POLICY pipeline_stage_instance_tenant ON marketplace.pipeline_stage_instance
    USING (
        EXISTS (
            SELECT 1
            FROM marketplace.loan_pipeline lp,
                 institution.current_member_ctx_v2() ctx
            WHERE lp.id = pipeline_stage_instance.pipeline_id
              AND lp.institution_id = ctx.institution_id
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM marketplace.loan_pipeline lp,
                 institution.current_member_ctx_v2() ctx
            WHERE lp.id = pipeline_stage_instance.pipeline_id
              AND lp.institution_id = ctx.institution_id
        )
    );

-- Ensure the authenticated role can still operate on these (RLS filters rows,
-- grants gate table access — both are needed).
GRANT SELECT, INSERT, UPDATE, DELETE ON institution.pipeline_template          TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON institution.pipeline_stage_def         TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON marketplace.loan_pipeline              TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON marketplace.pipeline_stage_instance    TO authenticated;
