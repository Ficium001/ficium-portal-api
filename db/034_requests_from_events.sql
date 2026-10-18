-- =============================================================================
-- 034_requests_from_events.sql — the marketplace learns about borrower requests
-- only from contract events (request.published / request.status_changed), not
-- by reading the App DB. Architecture rule: no direct link between the
-- borrower app and the portal.
--
-- 1. marketplace.ingest_request(...): ingest_app_request with ONE change: it
--    takes the borrower's contract id (anon_borrower_id, secret-keyed on the
--    borrower side) as given, instead of md5-hashing a raw App user id. Built
--    from ingest_app_request's own text so every other rule (status mapping,
--    window clamp, no-op skip, allocation diff) is identical by construction.
-- 2. integration.apply_request_live(request_id): request_shadow -> marketplace.
--    Returns true when the request is new (the caller notifies institutions).
-- 3. Re-key: marketplace.request and loan_pipeline move from the old id
--    (md5(app user id || ':ficium-anon-v1:'), unkeyed: anyone knowing a
--    borrower's App id could compute it) to the contract id.
-- 4. Backfill every request from the shadow, so live == shadow at the cut.
--
-- Run only after the App DB sync is stopped (else it would re-ingest with
-- the old id).
-- =============================================================================
BEGIN;

-- 1 ───────────────────────────────────────────────────────────────────────────
DO $mig$
DECLARE src text; dst text; n int;
BEGIN
  src := pg_get_functiondef('marketplace.ingest_app_request(uuid,uuid,text,numeric,integer,numeric,timestamptz,text,timestamptz,jsonb,text,jsonb)'::regprocedure);
  dst := replace(src, 'FUNCTION marketplace.ingest_app_request(', 'FUNCTION marketplace.ingest_request(');
  n := (length(dst) - length(replace(dst, 'v_anon_id := md5(p_consumer_id::text || '':ficium-anon-v1:'')::uuid;', '')))
       / length('v_anon_id := md5(p_consumer_id::text || '':ficium-anon-v1:'')::uuid;');
  IF n <> 1 THEN RAISE EXCEPTION 'expected one md5 line in ingest_app_request, found %', n; END IF;
  dst := replace(dst, 'v_anon_id := md5(p_consumer_id::text || '':ficium-anon-v1:'')::uuid;',
                      'v_anon_id := p_consumer_id;  -- contract id, as given (db/034)');
  IF dst = src OR position('ingest_request(' in dst) = 0 THEN RAISE EXCEPTION 'ingest_request not derived'; END IF;
  EXECUTE dst;
END $mig$;
COMMENT ON FUNCTION marketplace.ingest_request(uuid,uuid,text,numeric,integer,numeric,timestamptz,text,timestamptz,jsonb,text,jsonb) IS
  'db/034: ingest_app_request taking the contract anon_borrower_id as given (no md5). Fed only by contract events.';

-- 2 ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION integration.apply_request_live(p_request_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration, marketplace AS $$
DECLARE s integration.request_shadow%ROWTYPE; existed boolean;
BEGIN
  SELECT * INTO s FROM integration.request_shadow WHERE request_id = p_request_id;
  IF NOT FOUND THEN RETURN false; END IF;
  existed := EXISTS (SELECT 1 FROM marketplace.request WHERE id = p_request_id);
  PERFORM marketplace.ingest_request(
    s.request_id, s.anon_borrower_id, s.product_type, s.amount, s.term_months, s.max_rate,
    s.decision_deadline, s.status, s.created_at, coalesce(s.phase1, '{}'::jsonb),
    s.allocation_mode, s.allocations);
  RETURN NOT existed;
END $$;

-- 3 ───────────────────────────────────────────────────────────────────────────
DO $mig$
DECLARE orphans int;
BEGIN
  SELECT count(*) INTO orphans FROM marketplace.request r
   WHERE r.source = 'app' AND NOT EXISTS (SELECT 1 FROM integration.request_shadow s WHERE s.request_id = r.id);
  IF orphans > 0 THEN RAISE EXCEPTION '% app requests have no shadow row; cannot re-key them', orphans; END IF;
END $mig$;

UPDATE marketplace.request r
   SET consumer_id = s.anon_borrower_id, consumer_ref = left(s.anon_borrower_id::text, 8)
  FROM integration.request_shadow s
 WHERE s.request_id = r.id AND r.consumer_id IS DISTINCT FROM s.anon_borrower_id;

UPDATE marketplace.loan_pipeline lp
   SET consumer_id = r.consumer_id
  FROM marketplace.request r
 WHERE r.id = lp.request_id AND lp.consumer_id IS DISTINCT FROM r.consumer_id;

-- 4 ───────────────────────────────────────────────────────────────────────────
SELECT integration.apply_request_live(request_id) FROM integration.request_shadow;

REVOKE ALL ON FUNCTION marketplace.ingest_request(uuid,uuid,text,numeric,integer,numeric,timestamptz,text,timestamptz,jsonb,text,jsonb),
  integration.apply_request_live(uuid) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.ingest_request(uuid,uuid,text,numeric,integer,numeric,timestamptz,text,timestamptz,jsonb,text,jsonb),
      integration.apply_request_live(uuid) FROM anon, authenticated';
  END IF;
END $$;

COMMIT;
