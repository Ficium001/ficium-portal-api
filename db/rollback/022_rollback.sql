-- ROLLBACK for 022_schema_cleanup_step1 (Portal DB, egwobcajdlragubtkpqp).
-- Restores the exact pre-022 state. Safe to run any time BEFORE the retired schemas are hard-dropped
-- (not before 2026-11-06). Run in the SQL editor; it is all-or-nothing.
-- Original definitions are also kept in _rollback.snapshot WHERE migration = '022'.
BEGIN;

-- 3. app.current_institution_id() back to schema app (original ACL: postgres=UC, authenticated=U)
CREATE SCHEMA app AUTHORIZATION postgres;
GRANT USAGE ON SCHEMA app TO authenticated;
ALTER FUNCTION institution.current_institution_id() SET SCHEMA app;

-- 2. bid_notify back, with its trigger (original schema ACL: owner only)
ALTER SCHEMA _retired_bid_notify_20261006 RENAME TO bid_notify;
COMMENT ON SCHEMA bid_notify IS NULL;
CREATE TRIGGER trg_bid_notify AFTER INSERT ON marketplace.bid
  FOR EACH ROW EXECUTE FUNCTION bid_notify.on_bid_insert();

-- 1. workflow back (original ACL: postgres=UC, authenticated=U)
ALTER SCHEMA _retired_workflow_20261006 RENAME TO workflow;
COMMENT ON SCHEMA workflow IS NULL;
GRANT USAGE ON SCHEMA workflow TO authenticated;

COMMIT;

-- Verify after rollback:
--   SELECT tgname FROM pg_trigger WHERE tgrelid = 'marketplace.bid'::regclass AND tgname = 'trg_bid_notify';  -- 1 row
--   SELECT 'app.current_institution_id'::regproc;                                                           -- resolves
