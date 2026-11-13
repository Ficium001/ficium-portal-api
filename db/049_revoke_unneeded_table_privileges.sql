-- 049_revoke_unneeded_table_privileges.sql
-- Remove privileges no application path needs from the API roles.
-- TRUNCATE ignores RLS entirely; TRIGGER / REFERENCES / MAINTAIN are owner-level
-- operations. SELECT/INSERT/UPDATE/DELETE grants are untouched.
-- Found Nov 12 2026: Supabase defaults had given anon/authenticated these on
-- every public table (App DB: 38 objects; here: public.portal_notifications).
-- Idempotent. Mirrors the App DB migration of the same name (ficium repo).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.oid::regclass AS rel
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f')
  LOOP
    EXECUTE format('REVOKE TRUNCATE, REFERENCES, TRIGGER, MAINTAIN ON %s FROM anon, authenticated', r.rel);
  END LOOP;
END $$;

-- Tables created from now on (as postgres) no longer inherit them.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE TRUNCATE, REFERENCES, TRIGGER, MAINTAIN ON TABLES FROM anon, authenticated;
