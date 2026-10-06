-- ROLLBACK for 023_sync_loop_fix_ingest_app_request (Portal DB, egwobcajdlragubtkpqp).
-- Restores the pre-023 ingest function (idempotency_key arbiter, unconditional rewrite, 'closed' -> 'closed').
-- WARNING: that definition re-creates the write loop (and the 4 failing rows) the moment the 5-minute sync runs.
-- Only roll back if 023 itself misbehaves. The exact original text is also in
--   SELECT definition FROM _rollback.snapshot WHERE migration = '023' AND kind = 'function';
-- (prefer that copy if this file and the snapshot ever differ).
-- Data: the 4 rows corrected by the first post-023 sync are saved in _rollback.snapshot (kind = 'row'); restoring
-- them would put e83914bc/2fdc0931/8211ac9f/58a4bc11 back into the state the sync could not ingest, so it is not automatic.
BEGIN;
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
    WHEN 'closed'    THEN 'closed'
    ELSE 'bidding'
  END;
  v_close_at := COALESCE(p_deadline, p_created_at + interval '72 hours');

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
  ON CONFLICT (idempotency_key) DO UPDATE SET
    consumer_id = EXCLUDED.consumer_id, consumer_ref = EXCLUDED.consumer_ref, status = EXCLUDED.status,
    amount = EXCLUDED.amount, term_months = EXCLUDED.term_months, params = EXCLUDED.params, metadata = EXCLUDED.metadata,
    bid_window_closes_at = EXCLUDED.bid_window_closes_at,
    allocation_mode = EXCLUDED.allocation_mode, updated_at = now();

  IF p_allocations IS NOT NULL THEN
    DELETE FROM marketplace.request_allocation WHERE request_id = p_app_request_id;
      FOR v_alloc IN SELECT * FROM jsonb_array_elements(p_allocations) LOOP
        INSERT INTO marketplace.request_allocation (request_id, product_id, amount, sort_order)
        VALUES (p_app_request_id, catalog.product_id_for_app_type(v_alloc ->> 'product_type'),
                NULLIF(v_alloc ->> 'amount', '')::numeric, COALESCE((v_alloc ->> 'sort_order')::int, 0));
      END LOOP;
  END IF;
  RETURN p_app_request_id;
END;
$function$;

COMMIT;
