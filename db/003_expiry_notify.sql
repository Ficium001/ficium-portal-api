-- =============================================================================
-- close_expired_windows — updated to fire request-expired notifications
-- PORTAL DB (egwobcajdlragubtkpqp)
--
-- When a request closes with zero bids (status → 'expired'), fires pg_net
-- to /api/internal { action: 'request-expired' } so Vercel can write
-- a bid_expired notification to the consumer on the App DB.
--
-- Requests closing WITH bids (status → 'closed') are already covered by
-- per-bid notifications from trg_bid_notify. No duplicate notification sent.
-- =============================================================================

CREATE OR REPLACE FUNCTION marketplace.close_expired_windows()
RETURNS TABLE(request_id uuid, new_status text, bid_count bigint)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'marketplace', 'public', 'vault'
AS $$
DECLARE
  v_url    text;
  v_secret text;
  v_row    record;
BEGIN
  SELECT decrypted_secret INTO v_url    FROM vault.decrypted_secrets WHERE name = 'portal_api_url';
  SELECT decrypted_secret INTO v_secret FROM vault.decrypted_secrets WHERE name = 'app_service_secret';

  FOR v_row IN
    WITH expired AS (
      SELECT
        r.id,
        r.consumer_ref,
        COUNT(b.id) FILTER (WHERE b.status NOT IN ('withdrawn','rejected')) AS active_bids
      FROM marketplace.request r
      LEFT JOIN marketplace.bid b ON b.request_id = r.id
      WHERE r.status = 'bidding'
        AND r.bid_window_closes_at < now()
      GROUP BY r.id, r.consumer_ref
    ),
    updated AS (
      UPDATE marketplace.request r
      SET
        status     = CASE WHEN e.active_bids > 0 THEN 'closed' ELSE 'expired' END,
        updated_at = now()
      FROM expired e
      WHERE r.id = e.id
      RETURNING r.id,
                CASE WHEN e.active_bids > 0 THEN 'closed' ELSE 'expired' END AS new_status,
                e.active_bids,
                e.consumer_ref
    )
    SELECT u.id, u.new_status, u.active_bids, u.consumer_ref FROM updated u
  LOOP
    IF v_url IS NOT NULL AND v_secret IS NOT NULL AND v_row.new_status = 'expired' THEN
      PERFORM net.http_post(
        url     := 'https://ficium.vercel.app/api/internal',
        headers := jsonb_build_object(
          'Content-Type',     'application/json',
          'X-Service-Secret', v_secret
        ),
        body    := jsonb_build_object(
          'action',       'request-expired',
          'request_id',   v_row.id,
          'consumer_ref', v_row.consumer_ref,
          'bid_count',    v_row.active_bids
        ),
        timeout_milliseconds := 15000
      );
    END IF;

    request_id := v_row.id;
    new_status := v_row.new_status;
    bid_count  := v_row.active_bids;
    RETURN NEXT;
  END LOOP;
END; $$;
