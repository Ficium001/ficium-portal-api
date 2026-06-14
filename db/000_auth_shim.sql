-- =============================================================================
-- Ficium Portal — Portable auth shim
-- File: db/000_auth_shim.sql   (run FIRST, before the portal migrations)
--
-- WHY THIS EXISTS
-- ---------------
-- The Portal's entire security model — RLS policies, current_member_ctx(),
-- the maker-checker functions — resolves the caller's identity through
-- auth.uid() and auth.jwt(). On Supabase those helpers are provided by the
-- platform and PostgREST injects the verified JWT into the
-- `request.jwt.claims` GUC on every request.
--
-- Off Supabase (MRU-hosted Postgres, client cloud, on-premises) neither the
-- `auth` schema nor PostgREST exist. This shim recreates EXACTLY the contract
-- the SQL already depends on, so NONE of the existing policies or functions
-- need to change. The ficium-portal-api service sets `request.jwt.claims`
-- per request (see app/core/db.py), identical to what PostgREST did.
--
-- IDEMPOTENT and SAFE: only creates objects when absent. Do NOT run on a
-- Supabase-hosted database — there the platform owns these.
-- =============================================================================

-- ─── Roles PostgREST/Supabase assume exist ──────────────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
END
$$;

-- ─── auth schema + claim accessors ──────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS auth;

-- Full verified claim set as JSONB. Empty object when unauthenticated.
CREATE OR REPLACE FUNCTION auth.jwt()
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
  SELECT COALESCE(
    NULLIF(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  )
$$;

-- The authenticated subject (maps to ficium-auth user id = institution member
-- auth_user_id). NULL when unauthenticated — RLS then denies, fail-closed.
CREATE OR REPLACE FUNCTION auth.uid()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(
    current_setting('request.jwt.claims', true)::json ->> 'sub',
    ''
  )::uuid
$$;

-- auth.role() is referenced by some Supabase-generated policies; provide it too.
CREATE OR REPLACE FUNCTION auth.role()
RETURNS text
LANGUAGE sql
STABLE
AS $$
  SELECT COALESCE(
    current_setting('request.jwt.claims', true)::json ->> 'role',
    'anon'
  )
$$;

GRANT USAGE ON SCHEMA auth TO authenticated, anon;
GRANT EXECUTE ON FUNCTION auth.jwt(), auth.uid(), auth.role() TO authenticated, anon;

-- =============================================================================
-- Verification (run as a smoke test after loading the portal migrations):
--
--   SELECT set_config('request.jwt.claims',
--     '{"sub":"00000000-0000-0000-0000-000000000001","role":"authenticated"}',
--     true);
--   SELECT auth.uid();        -- → 00000000-0000-0000-0000-000000000001
--   SELECT auth.role();       -- → authenticated
--
-- With a real member row present, `SELECT * FROM institution.current_member_ctx()`
-- must then return that member's id + institution_id, proving the existing RLS
-- and maker-checker functions work unchanged against this shim.
-- =============================================================================
