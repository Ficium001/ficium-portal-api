-- =============================================================================
-- 055 — Admin command channel (admin site -> portal-api, HTTPS only)
--
-- The portal admin site has its own database (Neon, portal_admin) and never
-- connects to this one. It calls portal-api's /internal/admin/v1 endpoints,
-- HMAC-signed (Ficium-Signature, same scheme as the integration contract) and
-- carrying the acting staff member's ficium-auth MFA token. This file adds:
--
-- 1. integration.admin_command_log — one row per command_id: idempotency
--    (a retried command returns the stored result, never runs twice) and an
--    append-only record of who made / approved what.
-- 2. institution.doc.reviewed_by_staff — the reviewer as a ficium-auth user.
--    The old reviewed_by points at portal_admin.admin_users, which is retired
--    when the admin part leaves the portal.
-- =============================================================================

CREATE TABLE IF NOT EXISTS integration.admin_command_log (
  command_id   uuid        PRIMARY KEY,
  op           text        NOT NULL,
  body_sha256  text        NOT NULL,
  maker_sub    uuid        NOT NULL,
  checker_sub  uuid        NOT NULL,
  status_code  integer     NOT NULL,
  response     jsonb       NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION integration._admin_command_log_worm()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN RAISE EXCEPTION 'integration.admin_command_log is append-only' USING ERRCODE = '42501'; END $$;

DROP TRIGGER IF EXISTS admin_command_log_worm ON integration.admin_command_log;
CREATE TRIGGER admin_command_log_worm BEFORE UPDATE OR DELETE ON integration.admin_command_log
  FOR EACH ROW EXECUTE FUNCTION integration._admin_command_log_worm();
DROP TRIGGER IF EXISTS admin_command_log_worm_trunc ON integration.admin_command_log;
CREATE TRIGGER admin_command_log_worm_trunc BEFORE TRUNCATE ON integration.admin_command_log
  FOR EACH STATEMENT EXECUTE FUNCTION integration._admin_command_log_worm();

-- Only the API's service session writes it; no tenant role may read or write.
REVOKE ALL ON integration.admin_command_log FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION integration._admin_command_log_worm() FROM PUBLIC, anon, authenticated;

ALTER TABLE institution.doc
  ADD COLUMN IF NOT EXISTS reviewed_by_staff uuid REFERENCES auth_portal.auth_users(id);
