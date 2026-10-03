-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-03 as migration integration_acceptance_log_2026_10_03.
-- Step 5: idempotency log for POST /integration/v1/acceptances (app/api/acceptance.py, OFF by default).
-- A retried call with the same Idempotency-Key and the same body gets the stored answer (no second acceptance, no second
-- webhook); the same key with a different body is refused. Calls with one key are serialised with an advisory lock.
-- Verified before writing the endpoint (rolled back, live state untouched): marketplace.accept_bid on a real bid returns a
-- pipeline_id and a valid contact email, and flips the bid and request to accepted atomically.
CREATE TABLE IF NOT EXISTS integration.acceptance_log (
  idempotency_key text PRIMARY KEY CHECK (length(idempotency_key) BETWEEN 8 AND 200),
  body_sha256     text        NOT NULL,
  request_id      uuid,
  bid_id          uuid,
  status_code     integer     NOT NULL,
  response        jsonb       NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE integration.acceptance_log ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.acceptance_log FROM PUBLIC, anon, authenticated;
