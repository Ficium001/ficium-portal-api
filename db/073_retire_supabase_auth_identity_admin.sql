-- =============================================================================
-- 073 — Retire Supabase Auth, identity and admin on the Portal DB.
--
-- Portal logins go through ficium-auth (auth_portal); bank-side roles live in
-- portal_admin. Three older layers were left behind and nothing live uses them
-- (checked 2026-11-24: no code, function, view, policy or foreign key outside
-- them):
--   - Supabase Auth users: 3 test users from Jun 22 2026, last sign-in Jun 22,
--     each with an identity.profile row. Only those users are deleted.
--   - The on_auth_user_created trigger that copied new Supabase users into
--     identity.profile.
--   - identity (6 tables, 3 profile rows) and admin (5 tables: 2 users, 5 roles,
--     7 groups), both earlier generations of the admin identity model.
--
-- identity is dropped before the users are deleted: an append-only rule on
-- identity.login_event refuses the cascade a delete would cause.
-- Guarded, so a cell built on plain Postgres (no auth.users) runs it too.
--
-- Approved by KJ, Nov 24 2026. Database audit, 2026-11-24.
-- =============================================================================

do $m$
declare
  legacy_ids uuid[] := '{}';
begin
  if to_regclass('identity.profile') is not null then
    select coalesce(array_agg(id), '{}') into legacy_ids from identity.profile;
  end if;

  if to_regclass('auth.users') is not null then
    drop trigger if exists on_auth_user_created on auth.users;
  end if;

  drop schema if exists identity cascade;
  drop schema if exists admin cascade;

  if to_regclass('auth.users') is not null then
    delete from auth.users where id = any (legacy_ids);
  end if;
end $m$;
