-- =============================================================================
-- Ficium Portal — Loan Workflow Pipeline
-- File: db/001_workflow.sql
--
-- PURPOSE
-- -------
-- Post-bid loan processing workflow. Once a client accepts a bank's bid
-- (marketplace.acceptance), a workflow instance is created and the loan case
-- moves through a configurable pipeline: credit documentation → offer letter
-- → legal review → board/management approval.
--
-- DESIGN PRINCIPLES
-- -----------------
-- • Config-time tables hold institution-defined templates (stable).
-- • Run-time tables hold live case state (mutable, append-heavy).
-- • Template changes never break active workflow instances.
-- • RLS scopes every institution to its own data only.
-- • Idempotent: safe to run multiple times.
-- =============================================================================


-- ─── Schema ──────────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS workflow;

GRANT USAGE ON SCHEMA workflow TO authenticated;


-- =============================================================================
-- CONFIG-TIME TABLES
-- Institution admins set these up once; they define the pipeline shape.
-- =============================================================================


-- ─── workflow.template ───────────────────────────────────────────────────────
-- One template per institution × product type (e.g. MCB's SME Loan pipeline).
-- Institutions can have multiple templates per product type but only one
-- active one at a time.

CREATE TABLE IF NOT EXISTS workflow.template (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    institution_id   uuid        NOT NULL REFERENCES institution.institution(id) ON DELETE CASCADE,
    product_type     text        NOT NULL
                                 CHECK (product_type IN (
                                     'personal_loan', 'sme_loan', 'mortgage',
                                     'auto_loan', 'education_loan', 'general'
                                 )),
    name             text        NOT NULL,
    description      text,
    is_active        boolean     NOT NULL DEFAULT true,
    created_by       uuid        REFERENCES institution.member(id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Enforce single active template per institution × product_type
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_template_active
    ON workflow.template (institution_id, product_type)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_workflow_template_institution
    ON workflow.template (institution_id);


-- ─── workflow.stage ──────────────────────────────────────────────────────────
-- Ordered stages within a template. Stages run sequentially by default;
-- parallel_ok=true means the stage can start alongside the previous one.

CREATE TABLE IF NOT EXISTS workflow.stage (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id          uuid        NOT NULL REFERENCES workflow.template(id) ON DELETE CASCADE,
    position             integer     NOT NULL CHECK (position >= 1),
    name                 text        NOT NULL,
    stage_type           text        NOT NULL
                                     CHECK (stage_type IN (
                                         'credit_docs', 'offer_letter', 'legal_review',
                                         'approval_gate', 'custom'
                                     )),
    description          text,
    is_required          boolean     NOT NULL DEFAULT true,
    parallel_ok          boolean     NOT NULL DEFAULT false,  -- run alongside prev stage?
    sla_hours            integer     CHECK (sla_hours > 0),  -- NULL = no SLA
    escalate_after_hours integer     CHECK (escalate_after_hours > 0),
    created_at           timestamptz NOT NULL DEFAULT now(),

    UNIQUE (template_id, position)
);

CREATE INDEX IF NOT EXISTS idx_workflow_stage_template
    ON workflow.stage (template_id, position);


-- ─── workflow.stage_assignment ───────────────────────────────────────────────
-- Which department / officer handles a given stage.
-- assign_mode controls how the live case picks an officer when the stage opens.

CREATE TABLE IF NOT EXISTS workflow.stage_assignment (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    stage_id             uuid        NOT NULL REFERENCES workflow.stage(id) ON DELETE CASCADE,
    department           text,                                -- free-text label (e.g. "Credit Risk")
    assign_mode          text        NOT NULL DEFAULT 'manual'
                                     CHECK (assign_mode IN ('round_robin', 'manual', 'rule_based')),
    default_assignee_id  uuid        REFERENCES institution.member(id) ON DELETE SET NULL,
    requires_dual_sign   boolean     NOT NULL DEFAULT false,  -- maker-checker at this stage
    created_at           timestamptz NOT NULL DEFAULT now(),

    UNIQUE (stage_id)  -- one assignment config per stage
);


-- ─── workflow.doc_requirement ────────────────────────────────────────────────
-- Document checklist for a stage. A stage can gate advancement on all
-- is_mandatory docs being marked as received/verified.

CREATE TABLE IF NOT EXISTS workflow.doc_requirement (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    stage_id     uuid        NOT NULL REFERENCES workflow.stage(id) ON DELETE CASCADE,
    doc_key      text        NOT NULL,   -- internal key e.g. "payslip_3mo", "title_deed"
    label        text        NOT NULL,   -- display label e.g. "3 Months Payslips"
    is_mandatory boolean     NOT NULL DEFAULT true,
    position     integer     NOT NULL DEFAULT 1,
    created_at   timestamptz NOT NULL DEFAULT now(),

    UNIQUE (stage_id, doc_key)
);

CREATE INDEX IF NOT EXISTS idx_workflow_doc_stage
    ON workflow.doc_requirement (stage_id, position);


-- =============================================================================
-- RUN-TIME TABLES
-- Live state for an active loan case. Created on bid acceptance; mutated
-- as the case moves through stages.
-- =============================================================================


-- ─── workflow.instance ───────────────────────────────────────────────────────
-- One row per accepted bid. This is the loan case in the pipeline.
-- Links back to marketplace tables for full loan context.

CREATE TABLE IF NOT EXISTS workflow.instance (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    acceptance_id    uuid        NOT NULL UNIQUE REFERENCES marketplace.acceptance(id) ON DELETE RESTRICT,
    bid_id           uuid        NOT NULL REFERENCES marketplace.bid(id) ON DELETE RESTRICT,
    request_id       uuid        NOT NULL REFERENCES marketplace.request(id) ON DELETE RESTRICT,
    institution_id   uuid        NOT NULL REFERENCES institution.institution(id) ON DELETE RESTRICT,
    template_id      uuid        REFERENCES workflow.template(id) ON DELETE SET NULL,  -- null = ad-hoc
    current_stage_id uuid        REFERENCES workflow.stage(id) ON DELETE SET NULL,
    status           text        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN (
                                     'pending',      -- accepted, workflow not yet started
                                     'in_progress',  -- actively moving through stages
                                     'completed',    -- all stages done, loan approved
                                     'rejected',     -- failed at an approval gate
                                     'cancelled'     -- manually cancelled
                                 )),
    started_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,
    metadata         jsonb       NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflow_instance_institution
    ON workflow.instance (institution_id, status);

CREATE INDEX IF NOT EXISTS idx_workflow_instance_bid
    ON workflow.instance (bid_id);


-- ─── workflow.stage_action ───────────────────────────────────────────────────
-- Append-only audit trail. Every state change, document upload, note, or
-- approval on a stage generates one row here.

CREATE TABLE IF NOT EXISTS workflow.stage_action (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id  uuid        NOT NULL REFERENCES workflow.instance(id) ON DELETE CASCADE,
    stage_id     uuid        NOT NULL REFERENCES workflow.stage(id) ON DELETE RESTRICT,
    actor_id     uuid        REFERENCES institution.member(id) ON DELETE SET NULL,  -- null = system
    action       text        NOT NULL
                             CHECK (action IN (
                                 'opened',        -- stage became active
                                 'assigned',      -- officer assigned
                                 'doc_uploaded',  -- document received/uploaded
                                 'doc_verified',  -- document verified by officer
                                 'submitted',     -- stage submitted for review
                                 'approved',      -- stage approved (or co-signed in dual-sign)
                                 'rejected',      -- stage rejected
                                 'escalated',     -- SLA breach escalation fired
                                 'note_added'     -- free-text note
                             )),
    note         text,
    metadata     jsonb       NOT NULL DEFAULT '{}',  -- doc_key, escalation target, etc.
    occurred_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflow_stage_action_instance
    ON workflow.stage_action (instance_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_workflow_stage_action_actor
    ON workflow.stage_action (actor_id);


-- =============================================================================
-- RLS POLICIES
-- Institutions see only their own data. Admins see everything.
-- =============================================================================

ALTER TABLE workflow.template         ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow.stage            ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow.stage_assignment ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow.doc_requirement  ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow.instance         ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow.stage_action     ENABLE ROW LEVEL SECURITY;


-- Helper: resolve caller's institution_id from JWT claim
-- (portal-api sets request.jwt.claims per request)
CREATE OR REPLACE FUNCTION workflow._caller_institution_id()
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT (
        current_setting('request.jwt.claims', true)::jsonb ->> 'institution_id'
    )::uuid
$$;

-- Helper: is caller a Ficium admin?
CREATE OR REPLACE FUNCTION workflow._is_admin()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT COALESCE(
        (current_setting('request.jwt.claims', true)::jsonb ->> 'user_role')
        IN ('admin', 'super_admin'),
        false
    )
$$;


-- ── workflow.template ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS template_institution_isolation ON workflow.template;
CREATE POLICY template_institution_isolation ON workflow.template
    FOR ALL TO authenticated
    USING (
        workflow._is_admin()
        OR institution_id = workflow._caller_institution_id()
    )
    WITH CHECK (
        workflow._is_admin()
        OR institution_id = workflow._caller_institution_id()
    );

-- ── workflow.stage ────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS stage_institution_isolation ON workflow.stage;
CREATE POLICY stage_institution_isolation ON workflow.stage
    FOR ALL TO authenticated
    USING (
        workflow._is_admin()
        OR template_id IN (
            SELECT id FROM workflow.template
            WHERE institution_id = workflow._caller_institution_id()
        )
    );

-- ── workflow.stage_assignment ─────────────────────────────────────────────────
DROP POLICY IF EXISTS stage_assignment_isolation ON workflow.stage_assignment;
CREATE POLICY stage_assignment_isolation ON workflow.stage_assignment
    FOR ALL TO authenticated
    USING (
        workflow._is_admin()
        OR stage_id IN (
            SELECT s.id FROM workflow.stage s
            JOIN workflow.template t ON t.id = s.template_id
            WHERE t.institution_id = workflow._caller_institution_id()
        )
    );

-- ── workflow.doc_requirement ──────────────────────────────────────────────────
DROP POLICY IF EXISTS doc_req_isolation ON workflow.doc_requirement;
CREATE POLICY doc_req_isolation ON workflow.doc_requirement
    FOR ALL TO authenticated
    USING (
        workflow._is_admin()
        OR stage_id IN (
            SELECT s.id FROM workflow.stage s
            JOIN workflow.template t ON t.id = s.template_id
            WHERE t.institution_id = workflow._caller_institution_id()
        )
    );

-- ── workflow.instance ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS instance_institution_isolation ON workflow.instance;
CREATE POLICY instance_institution_isolation ON workflow.instance
    FOR ALL TO authenticated
    USING (
        workflow._is_admin()
        OR institution_id = workflow._caller_institution_id()
    )
    WITH CHECK (
        workflow._is_admin()
        OR institution_id = workflow._caller_institution_id()
    );

-- ── workflow.stage_action ─────────────────────────────────────────────────────
DROP POLICY IF EXISTS stage_action_isolation ON workflow.stage_action;
CREATE POLICY stage_action_isolation ON workflow.stage_action
    FOR ALL TO authenticated
    USING (
        workflow._is_admin()
        OR instance_id IN (
            SELECT id FROM workflow.instance
            WHERE institution_id = workflow._caller_institution_id()
        )
    );


-- =============================================================================
-- GRANTS
-- =============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA workflow TO authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA workflow TO authenticated;


-- =============================================================================
-- updated_at TRIGGER
-- =============================================================================

CREATE OR REPLACE FUNCTION workflow._set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS set_updated_at ON workflow.template;
CREATE TRIGGER set_updated_at
    BEFORE UPDATE ON workflow.template
    FOR EACH ROW EXECUTE FUNCTION workflow._set_updated_at();

DROP TRIGGER IF EXISTS set_updated_at ON workflow.instance;
CREATE TRIGGER set_updated_at
    BEFORE UPDATE ON workflow.instance
    FOR EACH ROW EXECUTE FUNCTION workflow._set_updated_at();


-- =============================================================================
-- CONVENIENCE VIEW
-- Flat view joining instance → acceptance → bid for portal case list queries.
-- =============================================================================

CREATE OR REPLACE VIEW workflow.v_case_summary AS
SELECT
    wi.id                  AS instance_id,
    wi.status              AS case_status,
    wi.institution_id,
    wi.started_at,
    wi.completed_at,
    -- marketplace context
    ma.id                  AS acceptance_id,
    ma.accepted_at,
    mb.id                  AS bid_id,
    mb.amount_offered,
    mb.rate,
    mb.rate_type,
    mb.term_months,
    mr.id                  AS request_id,
    mr.product_id,
    mr.amount              AS requested_amount,
    -- current stage
    ws.name                AS current_stage_name,
    ws.stage_type          AS current_stage_type,
    ws.position            AS current_stage_position,
    ws.sla_hours           AS current_stage_sla_hours
FROM workflow.instance wi
JOIN marketplace.acceptance ma ON ma.id = wi.acceptance_id
JOIN marketplace.bid        mb ON mb.id = wi.bid_id
JOIN marketplace.request    mr ON mr.id = wi.request_id
LEFT JOIN workflow.stage    ws ON ws.id = wi.current_stage_id;

GRANT SELECT ON workflow.v_case_summary TO authenticated;
