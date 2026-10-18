-- =============================================================================
-- 035_per_institution_decline.sql — contract 1.8.0.
--
-- An institution declining a request used to call the borrower database's
-- public.reject_request directly (a direct link), which closed the borrower's
-- request, and also set the marketplace request to 'cancelled' for EVERY
-- institution: the first bank to say no ended the request for all, even banks
-- with live offers. A decline is now per institution:
--   - the request leaves that institution's queue only;
--   - an institution with a live bid on it must withdraw instead;
--   - the borrower gets an anonymous request.declined event (no institution).
-- =============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS marketplace.request_decline (
    request_id     uuid        NOT NULL REFERENCES marketplace.request(id),
    institution_id uuid        NOT NULL,
    reason         text        CHECK (reason IS NULL OR char_length(reason) <= 500),
    declined_by    uuid,
    declined_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (request_id, institution_id)
);
ALTER TABLE marketplace.request_decline ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS request_decline_own ON marketplace.request_decline;
CREATE POLICY request_decline_own ON marketplace.request_decline FOR SELECT TO authenticated
    USING (institution_id = (SELECT institution_id FROM institution.current_member_ctx_v2()));
GRANT SELECT ON marketplace.request_decline TO authenticated;
REVOKE INSERT, UPDATE, DELETE ON marketplace.request_decline FROM authenticated;

CREATE OR REPLACE FUNCTION marketplace.decline_request(
    p_request_id uuid, p_institution_id uuid, p_actor uuid, p_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, marketplace, integration AS $$
DECLARE r marketplace.request%ROWTYPE; v_reason text := nullif(btrim(p_reason), ''); v_at timestamptz := now();
BEGIN
  SELECT * INTO r FROM marketplace.request WHERE id = p_request_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'DECLINE_NOT_FOUND'; END IF;
  IF r.status NOT IN ('open', 'bidding') THEN RAISE EXCEPTION 'DECLINE_NOT_OPEN'; END IF;
  IF EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.institution_id = p_institution_id
              AND b.status IN ('submitted', 'under_review')) THEN
    RAISE EXCEPTION 'DECLINE_HAS_BID';
  END IF;
  IF char_length(v_reason) > 500 THEN RAISE EXCEPTION 'DECLINE_REASON_TOO_LONG'; END IF;
  INSERT INTO marketplace.request_decline (request_id, institution_id, reason, declined_by, declined_at)
  VALUES (p_request_id, p_institution_id, v_reason, p_actor, v_at)
  ON CONFLICT (request_id, institution_id) DO NOTHING;
  IF NOT FOUND THEN RAISE EXCEPTION 'DECLINE_ALREADY'; END IF;
  -- Anonymous: the borrower learns that a provider declined, never which one.
  IF r.source = 'app' THEN
    PERFORM integration.enqueue('request.declined', p_request_id::text,
      jsonb_build_object('request_id', p_request_id, 'reason', v_reason, 'declined_at', v_at), 'institution');
  END IF;
  RETURN jsonb_build_object('request_id', p_request_id, 'declined_at', v_at);
END $$;
REVOKE ALL ON FUNCTION marketplace.decline_request(uuid, uuid, uuid, text) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.decline_request(uuid, uuid, uuid, text) FROM anon, authenticated';
  END IF;
END $$;

COMMIT;
