SELECT cron.unschedule('governance-expire-stale-actions');
DROP INDEX IF EXISTS governance.action_pending_expiry_idx;
-- expire_stale_actions() keeps working without the audit insert; restore the
-- pre-054 body only if audit rows for expiry are unwanted.
