-- =============================================================================
-- 037_last_links_to_events.sql — contract 1.10.0. The last direct links from
-- this side to the borrower app become contract events.
--
-- 1. close_expired_windows() no longer posts to the borrower app's /api/internal
--    (pg_net + the shared secret read from vault). For every app request whose
--    window closes it enqueues request.window_closed (offers / no_offers).
-- 2. integration.publish_market_intelligence(): the aggregate market data the
--    borrower app used to fetch from /public/market-intelligence with the
--    shared secret, as a market.intelligence event (rates as decimal fractions).
--    Called by the maintenance loop; skipped when nothing changed.
-- 0. Bug fix found on the way: close_expired_windows() sets 'closed' when a
--    window ends WITH bids (offers stay alive for the borrower), but the status
--    CHECK never allowed 'closed'. The first such request would fail the whole
--    run, so no window (not even empty ones) would close again. Allow it.
-- =============================================================================
BEGIN;

ALTER TABLE marketplace.request DROP CONSTRAINT IF EXISTS request_status_check;
ALTER TABLE marketplace.request ADD CONSTRAINT request_status_check
  CHECK (status = ANY (ARRAY['open','bidding','closed','accepted','cancelled','expired','withdrawn']));

CREATE OR REPLACE FUNCTION marketplace.close_expired_windows()
RETURNS TABLE(request_id uuid, new_status text, bid_count bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'marketplace', 'governance', 'integration', 'public' AS $function$
DECLARE v_row record;
BEGIN
  FOR v_row IN
    WITH expired AS (
      SELECT r.id, r.source, COUNT(b.id) FILTER (WHERE b.status NOT IN ('withdrawn','rejected')) AS active_bids
        FROM marketplace.request r
        LEFT JOIN marketplace.bid b ON b.request_id = r.id
       WHERE r.status = 'bidding' AND r.bid_window_closes_at < now()
       GROUP BY r.id, r.source
    ), updated AS (
      UPDATE marketplace.request r
         SET status = CASE WHEN e.active_bids > 0 THEN 'closed' ELSE 'expired' END, updated_at = now()
        FROM expired e WHERE r.id = e.id
      RETURNING r.id, CASE WHEN e.active_bids > 0 THEN 'closed' ELSE 'expired' END AS new_status, e.active_bids, e.source
    )
    SELECT u.id, u.new_status, u.active_bids, u.source FROM updated u
  LOOP
    -- Expire pending bid.submit governance actions for this request.
    UPDATE governance.action SET status = 'expired', updated_at = now()
     WHERE category = 'bid.submit' AND status = 'pending' AND payload->>'request_id' = v_row.id::text;
    -- Tell the borrower side through the contract (was an HTTP call with the shared secret).
    IF v_row.source = 'app' THEN
      PERFORM integration.enqueue('request.window_closed', v_row.id::text, jsonb_build_object(
        'request_id', v_row.id,
        'outcome', CASE WHEN v_row.active_bids > 0 THEN 'offers' ELSE 'no_offers' END,
        'offer_count', v_row.active_bids, 'closed_at', now()), 'institution');
    END IF;
    request_id := v_row.id; new_status := v_row.new_status; bid_count := v_row.active_bids;
    RETURN NEXT;
  END LOOP;
END;
$function$;

-- Catalog code -> contract product_type (same table as public.py's _APP_PRODUCT_TYPE). NULL when the contract
-- has no such type (e.g. education_loan): those rows are left out rather than failing validation.
CREATE OR REPLACE FUNCTION integration.app_type_for_product(p_code text)
RETURNS text LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT CASE WHEN t = ANY (ARRAY['sme_loan','personal_loan','mortgage','fixed_deposit','savings_account','credit_card',
                                  'business_account','investment_account','leasing','overdraft','business_loan','equities',
                                  'unit_trust','savings_plan','government_bonds','offshore_investment','mixed_portfolio'])
              THEN t END
    FROM (SELECT CASE p_code WHEN 'home_loan' THEN 'mortgage' WHEN 'deposit' THEN 'fixed_deposit'
                             WHEN 'savings' THEN 'savings_account' WHEN 'vehicle_loan' THEN 'leasing'
                             ELSE p_code END AS t) x;
$$;

CREATE OR REPLACE FUNCTION integration.build_market_intelligence()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, marketplace, catalog, integration AS $$
  WITH live AS (
    SELECT b.*, p.code AS product_code, r.product_id
      FROM marketplace.bid b JOIN marketplace.request r ON r.id = b.request_id JOIN catalog.product p ON p.id = r.product_id
     WHERE b.status IN ('submitted', 'accepted') AND b.created_at > now() - interval '90 days'),
  rates AS (
    SELECT jsonb_strip_nulls(jsonb_build_object(
      'product_code', integration.app_type_for_product(product_code),
      'bid_count', count(DISTINCT id), 'request_count', count(DISTINCT request_id),
      'min_rate', round(min(rate)::numeric, 6), 'max_rate', round(max(rate)::numeric, 6),
      'avg_rate', round(avg(rate)::numeric, 6),
      'p25_rate', round((percentile_cont(0.25) WITHIN GROUP (ORDER BY rate))::numeric, 6),
      'p75_rate', round((percentile_cont(0.75) WITHIN GROUP (ORDER BY rate))::numeric, 6))) AS j
      FROM live GROUP BY product_code),
  outcomes AS (
    SELECT jsonb_strip_nulls(jsonb_build_object(
      'product_code', integration.app_type_for_product(p.code), 'total_acceptances', count(*),
      'avg_winning_rate', round(avg(b.rate)::numeric, 6), 'min_winning_rate', round(min(b.rate)::numeric, 6),
      'max_winning_rate', round(max(b.rate)::numeric, 6), 'avg_winning_amount', round(avg(b.amount_offered)::numeric, 0),
      'avg_winning_term_months', greatest(1, round(avg(b.term_months))::int),
      'rate_vs_market_avg', round((avg(b.rate) - (SELECT avg(l.rate) FROM live l WHERE l.product_id = r.product_id))::numeric, 6))) AS j
      FROM marketplace.request r JOIN marketplace.bid b ON b.id = r.winning_bid_id JOIN catalog.product p ON p.id = r.product_id
     WHERE r.accepted_at > now() - interval '90 days' GROUP BY p.code, r.product_id),
  comp AS (
    SELECT jsonb_build_object('product_code', integration.app_type_for_product(product_code),
      'active_institutions', count(DISTINCT institution_id),
      'avg_bids_per_request', round((count(*)::numeric / nullif(count(DISTINCT request_id), 0)), 1),
      'max_bids_per_request', max(per_req)) AS j
      FROM (SELECT l.*, count(*) OVER (PARTITION BY l.request_id) AS per_req FROM live l) x GROUP BY product_code)
  SELECT jsonb_build_object('as_of', current_date,
    'rates', coalesce((SELECT jsonb_agg(j ORDER BY j ->> 'product_code') FROM rates WHERE j ? 'product_code'), '[]'::jsonb),
    'outcomes', coalesce((SELECT jsonb_agg(j ORDER BY j ->> 'product_code') FROM outcomes WHERE j ? 'product_code'), '[]'::jsonb),
    'competitiveness', coalesce((SELECT jsonb_agg(j ORDER BY j ->> 'product_code') FROM comp
                                  WHERE j ->> 'product_code' IS NOT NULL), '[]'::jsonb));
$$;

CREATE OR REPLACE FUNCTION integration.publish_market_intelligence()
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE d jsonb := integration.build_market_intelligence(); last_d jsonb;
BEGIN
  SELECT o.envelope -> 'data' INTO last_d FROM integration.outbox o
   WHERE o.aggregate_id = 'market' AND o.type = 'market.intelligence' ORDER BY o.sequence DESC LIMIT 1;
  IF last_d IS NOT NULL AND last_d - 'as_of' = d - 'as_of' AND last_d ->> 'as_of' = d ->> 'as_of' THEN
    RETURN false;  -- same data, same day
  END IF;
  PERFORM integration.enqueue('market.intelligence', 'market', d, 'institution');
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION integration.build_market_intelligence(), integration.publish_market_intelligence(),
  integration.app_type_for_product(text) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION integration.build_market_intelligence(), integration.publish_market_intelligence(),
      integration.app_type_for_product(text) FROM anon, authenticated';
  END IF;
END $$;

COMMIT;
