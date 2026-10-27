-- =============================================================================
-- 054 — Expired approvals must actually be EXPIRED, and the sweep must run
--
-- Bug: governance.action rows past expires_at stayed status='pending' forever.
-- governance.expire_stale_actions() existed but nothing ever called it (pg_cron
-- was not installed on the Portal DB); the only other writers flip a row to
-- 'expired' lazily, when someone tries to approve it. So the Inbox counted dead
-- actions as "To authorise", and a banner said "expiring within 4 hours" on
-- items that expired months ago.
--
-- 1. expire_stale_actions() now also writes one audit.event per row it expires
--    (every state change of an approval is audited; WORM log).
-- 2. Partial index so the sweep and the pending queue stay cheap.
-- 3. pg_cron job every 5 minutes. Reads do NOT depend on it: the API derives
--    the effective state (pending past expires_at = expired) in SQL.
-- 4. Backfill: run the sweep once now.
--
-- Not callable by API roles: only the cron job (postgres) runs it.
-- =============================================================================

CREATE OR REPLACE FUNCTION governance.expire_stale_actions()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'governance', 'audit', 'public'
AS $function$
DECLARE n integer;
BEGIN
  WITH expired AS (
    UPDATE governance.action
       SET status = 'expired', updated_at = now()
     WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < now()
    RETURNING id, category, institution_id, resource_type, resource_id, maker_id, expires_at
  ), logged AS (
    INSERT INTO audit.event
      (actor_type, institution_id, action, resource_type, resource_id,
       outcome, outcome_note, governance_action_id, metadata)
    SELECT 'system', e.institution_id, e.category || '.expired', e.resource_type, e.resource_id,
           'expired', 'Expired without a second approver', e.id,
           jsonb_build_object('maker_id', e.maker_id, 'expires_at', e.expires_at)
      FROM expired e
    RETURNING 1
  )
  SELECT count(*) INTO n FROM expired;
  RETURN n;
END
$function$;

REVOKE ALL ON FUNCTION governance.expire_stale_actions() FROM PUBLIC, anon, authenticated;

CREATE INDEX IF NOT EXISTS action_pending_expiry_idx
  ON governance.action (expires_at) WHERE status = 'pending';

CREATE EXTENSION IF NOT EXISTS pg_cron;
SELECT cron.schedule('governance-expire-stale-actions', '*/5 * * * *',
                     $$SELECT governance.expire_stale_actions()$$);

SELECT governance.expire_stale_actions();
