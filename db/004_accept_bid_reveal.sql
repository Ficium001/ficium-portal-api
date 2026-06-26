-- =============================================================================
-- accept_bid() — updated to include bid financials in return payload
-- PORTAL DB (egwobcajdlragubtkpqp)
--
-- Added to return: rate, rate_type, amount_offered, term_months
-- These are used by accept-bid.ts to write the bid_accepted notification
-- without an extra round-trip to fetch the bid.
-- Applied to production 2026-06-27.
-- =============================================================================

CREATE OR REPLACE FUNCTION marketplace.accept_bid(
  p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb
)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'marketplace', 'institution', 'public'
AS $$
DECLARE
  v_inst_id     uuid;
  v_inst        record;
  v_bid         record;
  v_pipeline_id uuid;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM marketplace.request
    WHERE id = p_request_id AND consumer_id = p_consumer_id
  ) THEN
    RAISE EXCEPTION 'Request % not found or not owned by consumer', p_request_id;
  END IF;

  SELECT * INTO v_bid FROM marketplace.bid
  WHERE id = p_bid_id AND request_id = p_request_id;

  IF v_bid.id IS NULL THEN
    RAISE EXCEPTION 'Bid % not found on request %', p_bid_id, p_request_id;
  END IF;

  v_inst_id := v_bid.institution_id;

  UPDATE marketplace.bid SET status = 'accepted', updated_at = now() WHERE id = p_bid_id;

  UPDATE marketplace.bid
  SET    status           = 'rejected',
         rejection_reason = 'Another offer was accepted by the client',
         rejected_at      = now(),
         updated_at       = now()
  WHERE  request_id = p_request_id
    AND  id        <> p_bid_id
    AND  status NOT IN ('withdrawn', 'rejected');

  UPDATE marketplace.request
  SET    status         = 'accepted',
         winning_bid_id = p_bid_id,
         accepted_at    = now(),
         updated_at     = now()
  WHERE  id = p_request_id;

  INSERT INTO marketplace.bid_acceptance (
    request_id, bid_id, institution_id,
    full_name, email, phone, address, date_of_birth, document_number
  ) VALUES (
    p_request_id, p_bid_id, v_inst_id,
    p_phase2 ->> 'full_name',  p_phase2 ->> 'email',
    p_phase2 ->> 'phone',      p_phase2 ->> 'address',
    (p_phase2 ->> 'date_of_birth')::date,
    p_phase2 ->> 'document_number'
  ) ON CONFLICT (request_id) DO NOTHING;

  BEGIN
    v_pipeline_id := marketplace.create_pipeline_from_acceptance(
      p_request_id, p_bid_id, v_inst_id, p_consumer_id
    );
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'Pipeline auto-create failed for bid %: %', p_bid_id, SQLERRM;
  END;

  SELECT name, legal_name, primary_contact_name,
         primary_contact_email, primary_contact_phone, logo_url
  INTO   v_inst
  FROM   institution.institution WHERE id = v_inst_id;

  RETURN jsonb_build_object(
    'institution_id',   v_inst_id,
    'institution_name', v_inst.name,
    'legal_name',       v_inst.legal_name,
    'contact_person',   v_inst.primary_contact_name,
    'contact_email',    v_inst.primary_contact_email,
    'contact_phone',    v_inst.primary_contact_phone,
    'logo_url',         v_inst.logo_url,
    'pipeline_id',      v_pipeline_id,
    'rate',             v_bid.rate,
    'rate_type',        v_bid.rate_type,
    'amount_offered',   v_bid.amount_offered,
    'term_months',      v_bid.term_months
  );
END; $$;
