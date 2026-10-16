-- =============================================================================
-- 025_deal_booking.sql — booking hand-off: the deal is booked in the bank's
-- core banking system.
--
-- WHY
--   A deal's last Ficium step was "pipeline completed". In a core banking
--   model the deal ends when the loan is BOOKED in the bank's system of
--   record (account/contract reference, booking date, disbursed amount).
--   That record is also what Ficium's commission is earned on.
--
--   Revenue leak found while building this: portal_admin.commission_event is
--   only written by marketplace.advance_pipeline_stage(), which portal-api
--   never calls (it completes stages in Python). Result: 1 completed deal,
--   0 commission rows. Booking now raises the commission event.
--
-- WHAT
--   1. marketplace.loan_booking — one row per deal; core reference unique per
--      institution. Tenant SELECT via RLS; writes only through 2.
--   2. marketplace.record_loan_booking() — validates, records or corrects the
--      booking, and raises / adjusts the commission event, atomically.
--      Errors are BOOKING_* codes mapped to HTTP by portal-api.
--   3. Permission inst:booking granted to every system and custom group that
--      already holds inst:pipeline.
--   4. portal_notifications.kind accepts 'deal_booked'.
--
-- CHANNELS
--   Portal  POST/PUT /pipelines/{id}/booking        (source 'portal')
--   API     POST/PUT /v1/pipeline/{id}/booking      (source 'api', pipeline:write)
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS marketplace.loan_booking (
    pipeline_id      uuid PRIMARY KEY REFERENCES marketplace.loan_pipeline(id) ON DELETE RESTRICT,
    institution_id   uuid NOT NULL,
    core_reference   text NOT NULL CHECK (char_length(btrim(core_reference)) BETWEEN 1 AND 64),
    booked_on        date NOT NULL,
    disbursed_amount numeric(18,2) NOT NULL CHECK (disbursed_amount > 0),
    disbursed_on     date,
    notes            text CHECK (notes IS NULL OR char_length(notes) <= 1000),
    source           text NOT NULL CHECK (source IN ('portal', 'api')),
    recorded_by      uuid,
    api_key_id       uuid,
    corrected_count  integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_loan_booking_core_reference UNIQUE (institution_id, core_reference)
);

ALTER TABLE marketplace.loan_booking ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace.loan_booking FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS loan_booking_tenant_read ON marketplace.loan_booking;
CREATE POLICY loan_booking_tenant_read ON marketplace.loan_booking
    FOR SELECT
    USING (
        institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)
    );

-- RLS needs a base grant (see db/README "RLS without a GRANT"). Read only:
-- every write goes through record_loan_booking() as postgres.
GRANT SELECT ON marketplace.loan_booking TO authenticated;

CREATE OR REPLACE FUNCTION marketplace.record_loan_booking(
    p_pipeline_id      uuid,
    p_institution_id   uuid,
    p_core_reference   text,
    p_booked_on        date,
    p_disbursed_amount numeric,
    p_disbursed_on     date,
    p_notes            text,
    p_source           text,
    p_actor_id         uuid,
    p_api_key_id       uuid,
    p_correction       boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'marketplace', 'portal_admin', 'pg_temp'
AS $fn$
DECLARE
    v_deal     marketplace.loan_pipeline%ROWTYPE;
    v_prev     marketplace.loan_booking%ROWTYPE;
    v_row      marketplace.loan_booking%ROWTYPE;
    v_ref      text := btrim(p_core_reference);
    v_today    date := (now() AT TIME ZONE 'Indian/Mauritius')::date;
    v_comm     text;
BEGIN
    SELECT * INTO v_deal FROM marketplace.loan_pipeline
     WHERE id = p_pipeline_id AND institution_id = p_institution_id
     FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'BOOKING_DEAL_NOT_FOUND'; END IF;
    IF v_deal.status <> 'completed' THEN RAISE EXCEPTION 'BOOKING_DEAL_NOT_COMPLETE'; END IF;

    IF v_ref IS NULL OR v_ref = '' OR char_length(v_ref) > 64 THEN
        RAISE EXCEPTION 'BOOKING_REFERENCE_INVALID';
    END IF;
    IF p_disbursed_amount IS NULL OR p_disbursed_amount <= 0 THEN
        RAISE EXCEPTION 'BOOKING_AMOUNT_INVALID';
    END IF;
    IF v_deal.deal_amount IS NOT NULL AND p_disbursed_amount > v_deal.deal_amount THEN
        RAISE EXCEPTION 'BOOKING_AMOUNT_EXCEEDS_DEAL';
    END IF;
    IF p_booked_on IS NULL OR p_booked_on > v_today THEN
        RAISE EXCEPTION 'BOOKING_DATE_INVALID';
    END IF;
    IF p_booked_on < v_deal.started_at::date THEN
        RAISE EXCEPTION 'BOOKING_DATE_BEFORE_DEAL';
    END IF;
    IF p_disbursed_on IS NOT NULL AND (p_disbursed_on > v_today OR p_disbursed_on < v_deal.started_at::date) THEN
        RAISE EXCEPTION 'BOOKING_DISBURSED_DATE_INVALID';
    END IF;
    IF p_source NOT IN ('portal', 'api') THEN RAISE EXCEPTION 'BOOKING_SOURCE_INVALID'; END IF;

    SELECT * INTO v_prev FROM marketplace.loan_booking WHERE pipeline_id = p_pipeline_id FOR UPDATE;
    IF FOUND AND NOT p_correction THEN RAISE EXCEPTION 'BOOKING_ALREADY_RECORDED'; END IF;
    IF NOT FOUND AND p_correction THEN RAISE EXCEPTION 'BOOKING_NOT_FOUND'; END IF;

    BEGIN
        IF p_correction THEN
            UPDATE marketplace.loan_booking
               SET core_reference   = v_ref,
                   booked_on        = p_booked_on,
                   disbursed_amount = p_disbursed_amount,
                   disbursed_on     = p_disbursed_on,
                   notes            = p_notes,
                   source           = p_source,
                   recorded_by      = p_actor_id,
                   api_key_id       = p_api_key_id,
                   corrected_count  = corrected_count + 1,
                   updated_at       = now()
             WHERE pipeline_id = p_pipeline_id
            RETURNING * INTO v_row;
        ELSE
            INSERT INTO marketplace.loan_booking
                (pipeline_id, institution_id, core_reference, booked_on, disbursed_amount,
                 disbursed_on, notes, source, recorded_by, api_key_id)
            VALUES
                (p_pipeline_id, p_institution_id, v_ref, p_booked_on, p_disbursed_amount,
                 p_disbursed_on, p_notes, p_source, p_actor_id, p_api_key_id)
            RETURNING * INTO v_row;
        END IF;
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION 'BOOKING_REFERENCE_IN_USE';
    END;

    -- Commission is earned on the booked (disbursed) amount. A correction
    -- re-bases it only while it hasn't been invoiced.
    INSERT INTO portal_admin.commission_event (pipeline_id, request_id, institution_id, deal_amount)
    VALUES (p_pipeline_id, v_deal.request_id, p_institution_id, p_disbursed_amount)
    ON CONFLICT (pipeline_id) DO UPDATE
        SET deal_amount = EXCLUDED.deal_amount, updated_at = now()
        WHERE portal_admin.commission_event.status = 'pending';
    SELECT status INTO v_comm FROM portal_admin.commission_event WHERE pipeline_id = p_pipeline_id;

    RETURN jsonb_build_object(
        'booking', to_jsonb(v_row),
        'previous', CASE WHEN p_correction THEN to_jsonb(v_prev) END,
        'commission_status', v_comm
    );
END;
$fn$;

-- Only portal-api (postgres) calls it. Never callable from the exposed API roles.
REVOKE ALL ON FUNCTION marketplace.record_loan_booking(uuid, uuid, text, date, numeric, date, text, text, uuid, uuid, boolean) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION marketplace.record_loan_booking(uuid, uuid, text, date, numeric, date, text, text, uuid, uuid, boolean) FROM anon, authenticated';
    END IF;
END $$;

ALTER TABLE public.portal_notifications DROP CONSTRAINT IF EXISTS portal_notifications_kind_check;
ALTER TABLE public.portal_notifications ADD CONSTRAINT portal_notifications_kind_check CHECK (kind = ANY (ARRAY[
    'bid_accepted', 'bid_rejected', 'bid_expired', 'request_closed', 'pipeline_advanced',
    'pipeline_approved', 'approval_needed', 'compliance_expiring', 'system', 'deal_booked'
]));

-- Permission: whoever runs deals can record their booking.
UPDATE portal_admin.user_groups
   SET module_permissions = array_append(module_permissions, 'inst:booking'), updated_at = now()
 WHERE 'inst:pipeline' = ANY(module_permissions)
   AND NOT ('inst:booking' = ANY(module_permissions));

UPDATE institution."group"
   SET module_permissions = array_append(module_permissions, 'inst:booking')
 WHERE 'inst:pipeline' = ANY(module_permissions)
   AND NOT ('inst:booking' = ANY(module_permissions));

COMMIT;
