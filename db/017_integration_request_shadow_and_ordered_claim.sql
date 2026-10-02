-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-02 as migration
-- integration_request_shadow_and_ordered_claim_2026_10_02.  Step 3 of the integration-contract migration, receiver side.
--
-- 1. ORDERING FIX in integration.claim_batch (affects every aggregate, both directions): event N+1 of an aggregate is
--    never sent before event N is delivered. Without it a retried older event is dropped as "stale" by the receiver's
--    watermark (found while designing step 3: request.published then request.status_changed share an aggregate).
--    A DEAD event blocks its aggregate on purpose (alert and resolve) rather than silently skipping it.
--    NOTE: the same function must be updated on the App DB before the borrower side starts emitting requests.
-- 2. integration.request_shadow: what the borrower side publishes. SHADOW MODE: nothing live reads it. No client-role access.
--    apply_request_published refuses any Phase 1 carrying an employer (defence in depth; the contract already does).
--    apply_request_status_changed refuses an unknown request so the sender retries instead of losing the change.
-- 3. integration.v_request_parity / _summary: compares the shadow copy with marketplace.request using the SAME
--    expressions as marketplace.ingest_app_request. The cutover exit criterion is: a week with zero mismatches.
--
-- VERIFIED (rolled back, against the 13 real live requests replayed as events built from the live rows themselves):
--   ordering: only event 1 claimable; blocked while 1 is failed/pending; claimable once 1 is delivered
--   parity: 13 shadow / 13 live / 12 match / 1 mismatch. The 1 is real drift, not a bug: an expired request carries a
--           legacy metadata key `income_band` that today's sync never writes. Deliberate changes (an amount, a Phase 1
--           field) are reported as `amount` / `metadata`. Decide: scrub that key, or exempt it in the view.
--   status for an unpublished request refused; event with an employer refused; client roles cannot read the shadow.

CREATE OR REPLACE FUNCTION integration.claim_batch(p_limit integer DEFAULT 20, p_lease_seconds integer DEFAULT 60)
RETURNS TABLE (id text, envelope jsonb, attempts integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
BEGIN
  RETURN QUERY
  UPDATE integration.outbox o
     SET status = 'sending',
         lease_until = now() + make_interval(secs => p_lease_seconds),
         attempts = o.attempts + 1
   WHERE o.id IN (
     SELECT x.id FROM integration.outbox x
      WHERE ((x.status = 'pending' AND x.next_attempt_at <= now())
          OR (x.status = 'sending' AND x.lease_until < now()))
        AND NOT EXISTS (SELECT 1 FROM integration.outbox e
                         WHERE e.aggregate_id = x.aggregate_id AND e.sequence < x.sequence AND e.status <> 'delivered')
      ORDER BY x.created_at
      LIMIT greatest(1, least(p_limit, 100))
      FOR UPDATE SKIP LOCKED)
  RETURNING o.id, o.envelope, o.attempts;
END $$;
REVOKE ALL ON FUNCTION integration.claim_batch(integer, integer) FROM PUBLIC, anon, authenticated;

CREATE TABLE IF NOT EXISTS integration.request_shadow (
  request_id        uuid PRIMARY KEY,
  anon_borrower_id  uuid        NOT NULL,
  product_type      text        NOT NULL,
  amount            numeric     NOT NULL,
  currency          char(3)     NOT NULL,
  term_months       integer,
  max_rate          numeric,
  decision_deadline timestamptz,
  allocation_mode   text,
  allocations       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  phase1            jsonb       NOT NULL,
  status            text        NOT NULL DEFAULT 'open',
  created_at        timestamptz NOT NULL,
  last_event_id     text        NOT NULL,
  last_sequence     bigint      NOT NULL,
  received_at       timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE integration.request_shadow ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.request_shadow FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION integration.apply_request_published(p_env jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE d jsonb := p_env -> 'data';
BEGIN
  IF d ? 'phase1' AND (d -> 'phase1') ? 'employer' THEN
    RAISE EXCEPTION 'employer must never be received';
  END IF;
  INSERT INTO integration.request_shadow AS s
    (request_id, anon_borrower_id, product_type, amount, currency, term_months, max_rate, decision_deadline,
     allocation_mode, allocations, phase1, created_at, last_event_id, last_sequence)
  VALUES ((d ->> 'request_id')::uuid, (d ->> 'anon_borrower_id')::uuid, d ->> 'product_type', (d ->> 'amount')::numeric,
          d ->> 'currency', NULLIF(d ->> 'term_months', '')::int, NULLIF(d ->> 'max_rate', '')::numeric,
          NULLIF(d ->> 'decision_deadline', '')::timestamptz, d ->> 'allocation_mode',
          coalesce(d -> 'allocations', '[]'::jsonb), d -> 'phase1', (d ->> 'created_at')::timestamptz,
          p_env ->> 'id', (p_env ->> 'sequence')::bigint)
  ON CONFLICT (request_id) DO UPDATE SET
    anon_borrower_id = EXCLUDED.anon_borrower_id, product_type = EXCLUDED.product_type, amount = EXCLUDED.amount,
    currency = EXCLUDED.currency, term_months = EXCLUDED.term_months, max_rate = EXCLUDED.max_rate,
    decision_deadline = EXCLUDED.decision_deadline, allocation_mode = EXCLUDED.allocation_mode,
    allocations = EXCLUDED.allocations, phase1 = EXCLUDED.phase1, created_at = EXCLUDED.created_at,
    last_event_id = EXCLUDED.last_event_id, last_sequence = EXCLUDED.last_sequence, updated_at = now();
END $$;

CREATE OR REPLACE FUNCTION integration.apply_request_status_changed(p_env jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE d jsonb := p_env -> 'data';
BEGIN
  UPDATE integration.request_shadow
     SET status = d ->> 'status', last_event_id = p_env ->> 'id', last_sequence = (p_env ->> 'sequence')::bigint, updated_at = now()
   WHERE request_id = (d ->> 'request_id')::uuid;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'status change for a request that was never published: %', d ->> 'request_id';
  END IF;
END $$;
REVOKE ALL ON FUNCTION integration.apply_request_published(jsonb), integration.apply_request_status_changed(jsonb) FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE VIEW integration.v_request_parity AS
WITH x AS (
  SELECT s.request_id, r.id AS live_id, s.last_sequence,
    s.amount = r.amount AS amount_ok,
    coalesce(s.term_months, 12) = r.term_months AS term_ok,
    (CASE s.status WHEN 'open' THEN 'bidding' WHEN 'accepted' THEN 'accepted' WHEN 'cancelled' THEN 'cancelled'
                   WHEN 'expired' THEN 'expired' WHEN 'closed' THEN 'closed' ELSE 'bidding' END) = r.status AS status_ok,
    coalesce(s.decision_deadline, s.created_at + interval '72 hours') = r.bid_window_closes_at AS window_ok,
    jsonb_strip_nulls(jsonb_build_object('app_product_type', s.product_type, 'max_rate', s.max_rate,
      'loan_purpose', s.phase1 -> 'loan_purpose', 'collateral_type', s.phase1 -> 'collateral_type',
      'collateral_sub', s.phase1 -> 'collateral_sub', 'ltv_pct', s.phase1 -> 'ltv_pct')) = r.params AS params_ok,
    jsonb_strip_nulls(jsonb_build_object('ficium_attested', true,
      'kyc_verified', s.phase1 -> 'kyc_verified', 'employment_status', s.phase1 -> 'employment_status',
      'employment_type', s.phase1 -> 'employment_type', 'years_employed', s.phase1 -> 'years_employed',
      'gross_monthly_income', s.phase1 -> 'gross_monthly_income', 'income_verified', s.phase1 -> 'income_verified',
      'dsr_current_pct', s.phase1 -> 'dsr_current_pct', 'dsr_post_pct', s.phase1 -> 'dsr_post_pct',
      'net_worth_band', s.phase1 -> 'net_worth_band', 'has_existing_loans', s.phase1 -> 'has_existing_loans',
      'existing_monthly_repayment', s.phase1 -> 'existing_monthly_repayment', 'existing_loan_balance', s.phase1 -> 'existing_loan_balance',
      'loan_breakdown', s.phase1 -> 'loan_breakdown', 'health_score', s.phase1 -> 'health_score', 'risk_score', s.phase1 -> 'risk_score',
      'affordability_score', s.phase1 -> 'affordability_score', 'risk_tier', s.phase1 -> 'risk_tier', 'age', s.phase1 -> 'age',
      'risk_appetite', s.phase1 -> 'risk_appetite', 'investment_horizon', s.phase1 -> 'investment_horizon',
      'liquidity_pref', s.phase1 -> 'liquidity_pref', 'investment_style', s.phase1 -> 'investment_style',
      'target_amount', s.phase1 -> 'target_amount', 'monthly_contribution', s.phase1 -> 'monthly_contribution',
      'investment_objective', s.phase1 -> 'investment_objective', 'investment_product_answers', s.phase1 -> 'investment_product_answers')) = r.metadata AS metadata_ok,
    (SELECT count(*) FROM marketplace.request_allocation a WHERE a.request_id = s.request_id) = jsonb_array_length(s.allocations) AS allocations_ok
  FROM integration.request_shadow s LEFT JOIN marketplace.request r ON r.id = s.request_id
)
SELECT request_id, live_id IS NOT NULL AS in_live, last_sequence,
       array_remove(ARRAY[CASE WHEN NOT amount_ok THEN 'amount' END, CASE WHEN NOT term_ok THEN 'term_months' END,
                          CASE WHEN NOT status_ok THEN 'status' END, CASE WHEN NOT window_ok THEN 'bid_window' END,
                          CASE WHEN NOT params_ok THEN 'params' END, CASE WHEN NOT metadata_ok THEN 'metadata' END,
                          CASE WHEN NOT allocations_ok THEN 'allocations' END], NULL) AS mismatches,
       live_id IS NOT NULL AND coalesce(amount_ok AND term_ok AND status_ok AND window_ok AND params_ok AND metadata_ok AND allocations_ok, false) AS matches
FROM x;

CREATE OR REPLACE VIEW integration.v_request_parity_summary AS
SELECT (SELECT count(*) FROM integration.request_shadow) AS shadow_rows,
       (SELECT count(*) FROM marketplace.request) AS live_rows,
       (SELECT count(*) FROM integration.v_request_parity WHERE matches) AS matching,
       (SELECT count(*) FROM integration.v_request_parity WHERE in_live AND NOT matches) AS mismatching,
       (SELECT count(*) FROM integration.v_request_parity WHERE NOT in_live) AS shadow_not_in_live,
       (SELECT count(*) FROM marketplace.request r WHERE NOT EXISTS (SELECT 1 FROM integration.request_shadow s WHERE s.request_id = r.id)) AS live_not_in_shadow;
REVOKE ALL ON integration.v_request_parity, integration.v_request_parity_summary FROM PUBLIC, anon, authenticated;
