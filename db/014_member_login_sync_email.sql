-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-02 as migration
-- member_login_sync_email_2026_10_02. Extends 013: the trigger now mirrors member.email as well.
--
-- Why: tenant sessions (role authenticated) cannot write auth_portal.auth_users (RLS, no policies), so
-- portal-api's "change member email" (members.py, approvals.py) silently updated 0 login rows. The login
-- email is what ficium-auth authenticates against, so member and login drifted apart.
-- Now: member.email -> auth_users.email (trimmed, lower-cased) in the same transaction. auth_users.email
-- is globally UNIQUE (uq_auth_users_email), so an email another login already uses is REFUSED with a
-- unique violation; portal-api turns that into HTTP 409.
--
-- VERIFIED (all rolled back, live data untouched):
--   email change mirrors to the login (trimmed, lower-case); collision -> unique_violation 23505;
--   unrelated member updates and same-value "changes" leave the login row untouched (row version check);
--   real path: tenant session as an institution admin -> 1 member row changed and login email follows;
--   and the exact provisioning SQL used by the new provision_user_from_action runs clean on the real schema.

CREATE OR REPLACE FUNCTION institution.sync_member_login_state()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $fn$
BEGIN
  IF NEW.auth_user_id IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.active IS DISTINCT FROM OLD.active THEN
    UPDATE auth_portal.auth_users SET is_active = NEW.active, updated_at = now() WHERE id = NEW.auth_user_id;
  END IF;
  IF NEW.email IS DISTINCT FROM OLD.email AND NEW.email IS NOT NULL THEN
    UPDATE auth_portal.auth_users SET email = lower(btrim(NEW.email)), updated_at = now() WHERE id = NEW.auth_user_id;
  END IF;
  RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION institution.sync_member_login_state() FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_member_sync_login_state ON institution.member;
CREATE TRIGGER trg_member_sync_login_state
  AFTER UPDATE OF active, email ON institution.member
  FOR EACH ROW EXECUTE FUNCTION institution.sync_member_login_state();
