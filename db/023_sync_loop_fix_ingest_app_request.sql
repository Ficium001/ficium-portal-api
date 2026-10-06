-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-06 as migration 023_sync_loop_fix_ingest_app_request.
-- Rollback: db/rollback/023_rollback.sql (old definition + the 4 affected rows are also in
-- _rollback.snapshot WHERE migration = '023').
--
-- PROBLEM (measured 2026-10-06): marketplace.request had 180,887 writes for 13 rows and request_allocation
-- 236,159 deletes + 236,171 inserts for 12 rows since 2026-05-22. Cause: POST /marketplace/sync-requests runs
-- every 5 min (pg_cron on the App DB) and advances its (updated_at, id) watermark only across a contiguous run
-- of successes. Four rows failed on EVERY run, so the watermark sat at 1970 and all 13 rows were re-ingested
-- every 5 minutes, each one rewritten even when unchanged.
--   e83914bc  App status 'closed' -> request_status_check rejects it; AND its legacy idempotency_key
--             ('app_sync_...', first sync version) made the upsert miss it and collide on the primary key.
--   2fdc0931, 8211ac9f, 58a4bc11
--             opened in the Portal on 2026-07-17 with bid_window_opens_at AFTER the App's original deadline;
--             every sync tried closes_at = App deadline -> request_check (closes > opens). They were also
--             still 'bidding' in the Portal while expired in the App.
--
-- CHANGES to marketplace.ingest_app_request():
--   1. App 'closed' maps to 'expired' (terminal, relistable; what the Portal already held for e83914bc).
--   2. Upsert arbiter is the primary key (id) instead of idempotency_key.
--   3. Window start is repaired only when it would break closes > opens; closes is clamped to >= created + 1s.
--   4. DO UPDATE ... WHERE (...) IS DISTINCT FROM (...): unchanged rows are not rewritten.
--   5. Allocations are deleted/re-inserted only when the set actually changed.
-- Verified before applying (rolled back) and after (live): see the PR description.
--
-- NOT changed: the sync endpoint's fail-closed watermark. A future poison row will again block the watermark,
-- but it now costs reads only (no writes) and is reported by the 207/500 status and /marketplace/sync-health.

-- (snapshot INSERT into _rollback.snapshot omitted here: it ran in the migration, see the table)

CREATE OR REPLACE FUNCTION marketplace.ingest_app_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb DEFAULT '{}'::jsonb, p_allocation_mode text DEFAULT NULL::text, p_allocations jsonb DEFAULT NULL::jsonb)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'marketplace', 'catalog', 'public'
AS $function$
DECLARE
  v_product_id uuid; v_status text; v_close_at timestamptz; v_anon_id uuid; v_alloc jsonb; v_alloc_changed boolean;
BEGIN
  v_product_id := catalog.product_id_for_app_type(p_app_product_type);
  IF v_product_id IS NULL THEN
    RAISE EXCEPTION 'No catalog product for app type: %', p_app_product_type;
  END IF;
  v_anon_id := md5(p_consumer_id::text || ':ficium-anon-v1:')::uuid;
  v_status := CASE p_status
    WHEN 'open'      THEN 'bidding'
    WHEN 'accepted'  THEN 'accepted'
    WHEN 'cancelled' THEN 'cancelled'
    WHEN 'expired'   THEN 'expired'
    WHEN 'closed'    THEN 'expired'
    ELSE 'bidding'
  END;
  v_close_at := GREATEST(COALESCE(p_deadline, p_created_at + interval '72 hours'), p_created_at + interval '1 second');

  INSERT INTO marketplace.request AS r (
    id, consumer_id, consumer_ref, product_id, country, currency, amount, term_months,
    params, status, bid_window_opens_at, bid_window_closes_at,
    idempotency_key, source, metadata, created_at, allocation_mode
  ) VALUES (
    p_app_request_id, v_anon_id, LEFT(v_anon_id::text, 8), v_product_id, 'MU', 'MUR',
    p_amount, COALESCE(p_term_months, 12),
    jsonb_strip_nulls(jsonb_build_object(
      'app_product_type', p_app_product_type, 'max_rate', p_max_rate,
      'loan_purpose', p_phase1 -> 'loan_purpose', 'collateral_type', p_phase1 -> 'collateral_type',
      'collateral_sub', p_phase1 -> 'collateral_sub', 'ltv_pct', p_phase1 -> 'ltv_pct')),
    v_status, p_created_at, v_close_at, p_app_request_id::text, 'app',
    jsonb_strip_nulls(jsonb_build_object(
      'ficium_attested', true, 'kyc_verified', p_phase1 -> 'kyc_verified',
      'employment_status', p_phase1 -> 'employment_status', 'employment_type', p_phase1 -> 'employment_type',
      -- 'employer' deliberately excluded (re-identification risk).
      'years_employed', p_phase1 -> 'years_employed', 'gross_monthly_income', p_phase1 -> 'gross_monthly_income',
      'income_verified', p_phase1 -> 'income_verified', 'dsr_current_pct', p_phase1 -> 'dsr_current_pct',
      'dsr_post_pct', p_phase1 -> 'dsr_post_pct', 'net_worth_band', p_phase1 -> 'net_worth_band',
      'has_existing_loans', p_phase1 -> 'has_existing_loans', 'existing_monthly_repayment', p_phase1 -> 'existing_monthly_repayment',
      'existing_loan_balance', p_phase1 -> 'existing_loan_balance', 'loan_breakdown', p_phase1 -> 'loan_breakdown',
      'health_score', p_phase1 -> 'health_score', 'risk_score', p_phase1 -> 'risk_score',
      'affordability_score', p_phase1 -> 'affordability_score', 'risk_tier', p_phase1 -> 'risk_tier',
      'age', p_phase1 -> 'age', 'risk_appetite', p_phase1 -> 'risk_appetite',
      'investment_horizon', p_phase1 -> 'investment_horizon', 'liquidity_pref', p_phase1 -> 'liquidity_pref',
      'investment_style', p_phase1 -> 'investment_style', 'target_amount', p_phase1 -> 'target_amount',
      'monthly_contribution', p_phase1 -> 'monthly_contribution', 'investment_objective', p_phase1 -> 'investment_objective',
      'investment_product_answers', p_phase1 -> 'investment_product_answers')),
    p_created_at, p_allocation_mode
  )
  ON CONFLICT (id) DO UPDATE SET
    consumer_id = EXCLUDED.consumer_id, consumer_ref = EXCLUDED.consumer_ref, status = EXCLUDED.status,
    amount = EXCLUDED.amount, term_months = EXCLUDED.term_months, params = EXCLUDED.params, metadata = EXCLUDED.metadata,
    bid_window_opens_at = CASE WHEN EXCLUDED.bid_window_closes_at <= r.bid_window_opens_at
                               THEN EXCLUDED.bid_window_opens_at ELSE r.bid_window_opens_at END,
    bid_window_closes_at = EXCLUDED.bid_window_closes_at,
    allocation_mode = EXCLUDED.allocation_mode, updated_at = now()
  WHERE (r.consumer_id, r.consumer_ref, r.status, r.amount, r.term_months, r.params, r.metadata, r.bid_window_closes_at, r.allocation_mode)
        IS DISTINCT FROM
        (EXCLUDED.consumer_id, EXCLUDED.consumer_ref, EXCLUDED.status, EXCLUDED.amount, EXCLUDED.term_months, EXCLUDED.params, EXCLUDED.metadata, EXCLUDED.bid_window_closes_at, EXCLUDED.allocation_mode)
     OR r.bid_window_opens_at >= EXCLUDED.bid_window_closes_at;

  IF p_allocations IS NOT NULL THEN
    WITH incoming AS (
      SELECT catalog.product_id_for_app_type(a ->> 'product_type') AS product_id,
             NULLIF(a ->> 'amount', '')::numeric AS amount, COALESCE((a ->> 'sort_order')::int, 0) AS sort_order
        FROM jsonb_array_elements(p_allocations) a),
    existing AS (SELECT product_id, amount, sort_order FROM marketplace.request_allocation WHERE request_id = p_app_request_id)
    SELECT EXISTS (SELECT * FROM incoming EXCEPT SELECT * FROM existing)
        OR EXISTS (SELECT * FROM existing EXCEPT SELECT * FROM incoming)
        OR (SELECT count(*) FROM incoming) <> (SELECT count(*) FROM existing)
      INTO v_alloc_changed;
    IF v_alloc_changed THEN
      DELETE FROM marketplace.request_allocation WHERE request_id = p_app_request_id;
      FOR v_alloc IN SELECT * FROM jsonb_array_elements(p_allocations) LOOP
        INSERT INTO marketplace.request_allocation (request_id, product_id, amount, sort_order)
        VALUES (p_app_request_id, catalog.product_id_for_app_type(v_alloc ->> 'product_type'),
                NULLIF(v_alloc ->> 'amount', '')::numeric, COALESCE((v_alloc ->> 'sort_order')::int, 0));
      END LOOP;
    END IF;
  END IF;
  RETURN p_app_request_id;
END;
$function$;
