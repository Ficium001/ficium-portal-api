-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-02 as migration
-- member_login_sync_and_anon_table_lockdown_2026_10_02.
--
-- 1. LATENT BUG (found in review): deactivating a member did not disable their login.
--    ficium-auth allows login and token refresh ONLY while auth_portal.auth_users.is_active is true
--    (api/auth.py login + refresh); it never reads institution.member.active. portal-api's
--    deactivate/reactivate/update-email/approve-user endpoints run in tenant sessions (role
--    `authenticated`), and auth_portal has RLS on with ZERO policies, so their UPDATEs on
--    auth_users silently affect 0 rows (their INSERT is refused). Proven on the live DB:
--    6 logins exist; a tenant session sees 0, updates 0, INSERT -> 42501.
--    Not exploited today (0 of 4 members ever deactivated), but a departed bank employee would
--    have kept access. Fix at the DB layer so it holds for every code path: a SECURITY DEFINER
--    trigger copies member.active -> auth_users.is_active in the same transaction.
--
-- 2. DEFENCE IN DEPTH: `anon` held full SELECT/INSERT/UPDATE/DELETE on every auth_portal table
--    (auth_users with password_hash and mfa_secret, auth_sessions with refresh_token_hash, reset
--    tokens, MFA backup codes) and USAGE on the schema, protected only by RLS-with-no-policies.
--    Over HTTPS the public key got 200 [] on all of them; one added policy would have exposed
--    them. Nothing legitimate uses anon there (SPA never references auth_portal; ficium-auth
--    uses the service key). Also removed anon from public.portal_notifications and
--    public._identity_migration_log, and `authenticated` from the unused migration log.
--
-- VERIFIED AFTER APPLY (all rolled back, live data untouched):
--   * trigger as owner: deactivate -> login false, reactivate -> login true
--   * real path: tenant session (role authenticated) as an institution admin updates the member
--     -> 1 row changed and login is_active = false in the same transaction
--   * anon: 0 privileges on auth_portal tables, no schema USAGE, no access to the 2 public tables
--   * service_role keeps read+write on auth_users; authenticated keeps portal_notifications
--   * sweep of every table readable by `authenticated` under real member claims: 93/93 before,
--     92/92 after (the one fewer is _identity_migration_log, revoked on purpose), 0 failures
--   * HTTPS with the public key, before -> after: auth_users, auth_sessions, portal_notifications,
--     _identity_migration_log   200 [] -> 401 (42501) for all four
--
-- NOT DONE YET (needs a portal-api code change, so it is deliberately left for a follow-up):
--   `authenticated` still holds SIUD grants on auth_portal tables (RLS still blocks every row).
--   members.py (list/get/update/deactivate/reactivate) and approvals.py (execute_user_update,
--   provision_user_from_action) still touch auth_users from tenant sessions; revoking now would
--   turn their silent no-ops into 500s. Move those statements to service_session(), THEN revoke.
--   Also unfixed in code: email change on a member does not reach auth_users, and approving a
--   new-user request cannot create the login row (INSERT is refused for tenant sessions).

CREATE OR REPLACE FUNCTION institution.sync_member_login_state()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $fn$
BEGIN
  IF NEW.auth_user_id IS NOT NULL AND NEW.active IS DISTINCT FROM OLD.active THEN
    UPDATE auth_portal.auth_users SET is_active = NEW.active, updated_at = now() WHERE id = NEW.auth_user_id;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION institution.sync_member_login_state() FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_member_sync_login_state ON institution.member;
CREATE TRIGGER trg_member_sync_login_state
  AFTER UPDATE OF active ON institution.member
  FOR EACH ROW EXECUTE FUNCTION institution.sync_member_login_state();

REVOKE ALL ON ALL TABLES    IN SCHEMA auth_portal FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA auth_portal FROM anon;
REVOKE USAGE ON SCHEMA auth_portal FROM anon;
REVOKE ALL ON public.portal_notifications FROM anon;
REVOKE ALL ON public._identity_migration_log FROM anon, authenticated;

DO $$
BEGIN
  IF has_schema_privilege('anon', 'auth_portal', 'USAGE') THEN
    RAISE EXCEPTION 'anon still has USAGE on auth_portal';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'auth_portal' AND c.relkind IN ('r','p','v','m')
               AND (has_table_privilege('anon', c.oid, 'SELECT') OR has_table_privilege('anon', c.oid, 'INSERT')
                 OR has_table_privilege('anon', c.oid, 'UPDATE') OR has_table_privilege('anon', c.oid, 'DELETE'))) THEN
    RAISE EXCEPTION 'anon still has a privilege on an auth_portal table';
  END IF;
  IF has_table_privilege('anon', 'public.portal_notifications', 'SELECT') OR has_table_privilege('anon', 'public._identity_migration_log', 'SELECT') THEN
    RAISE EXCEPTION 'anon still has access to a public table';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_member_sync_login_state' AND tgrelid = 'institution.member'::regclass) THEN
    RAISE EXCEPTION 'login sync trigger missing';
  END IF;
END $$;
