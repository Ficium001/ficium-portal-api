-- Synthetic mirror of the real schema shape, for testing the backfill script.

CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS auth_portal;
CREATE SCHEMA IF NOT EXISTS institution;
CREATE SCHEMA IF NOT EXISTS portal_admin;

-- Old identity space (Supabase)
CREATE TABLE auth.users (
  id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL
);

-- New identity space (ficium-auth)
CREATE TABLE auth_portal.auth_users (
  id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL
);

-- Tenant tables referencing the OLD identity, mirroring real shape
CREATE TABLE institution.institution_members (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id  UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  is_primary_admin BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE portal_admin.admin_users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id  UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  role_slug     TEXT NOT NULL DEFAULT 'support'
);

-- ── Test data ─────────────────────────────────────────────────────────────
-- Case 1: mcbadmin — exists in both systems, needs migration (the real bug)
WITH s AS (INSERT INTO auth.users (email) VALUES ('mcbadmin@mcb.mu') RETURNING id),
     f AS (INSERT INTO auth_portal.auth_users (email) VALUES ('mcbadmin@mcb.mu') RETURNING id)
INSERT INTO institution.institution_members (auth_user_id, is_primary_admin)
SELECT s.id, true FROM s;

-- Case 2: a second institution member, also needs migration
WITH s AS (INSERT INTO auth.users (email) VALUES ('analyst@mcb.mu') RETURNING id),
     f AS (INSERT INTO auth_portal.auth_users (email) VALUES ('analyst@mcb.mu') RETURNING id)
INSERT INTO institution.institution_members (auth_user_id, is_primary_admin)
SELECT s.id, false FROM s;

-- Case 3: an admin_users row, needs migration
WITH s AS (INSERT INTO auth.users (email) VALUES ('ops@ficium.mu') RETURNING id),
     f AS (INSERT INTO auth_portal.auth_users (email) VALUES ('ops@ficium.mu') RETURNING id)
INSERT INTO portal_admin.admin_users (auth_user_id, role_slug)
SELECT s.id, 'super_admin' FROM s;

-- Case 4: a user who exists in Supabase but NEVER logged into ficium-auth
-- (no matching auth_portal.auth_users row) — must show as UNMATCHED, not crash.
WITH s AS (INSERT INTO auth.users (email) VALUES ('stale@mcb.mu') RETURNING id)
INSERT INTO institution.institution_members (auth_user_id, is_primary_admin)
SELECT s.id, false FROM s;

-- Case 5: email case mismatch (Supabase stored mixed case, ficium-auth lowercase)
-- — must still resolve via the lower() normalisation in the script.
WITH s AS (INSERT INTO auth.users (email) VALUES ('Mixed.Case@MCB.mu') RETURNING id),
     f AS (INSERT INTO auth_portal.auth_users (email) VALUES ('mixed.case@mcb.mu') RETURNING id)
INSERT INTO institution.institution_members (auth_user_id, is_primary_admin)
SELECT s.id, false FROM s;
