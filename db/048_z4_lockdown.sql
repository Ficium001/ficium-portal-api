-- 048_z4_lockdown.sql   (applied 2026-10-23, the last step of Z4)
-- Apply only after ALL of these are live and verified:
--   portal-api z4/one-identity (registration, onboarding, signed document
--   links), ficium-portal z4/one-identity (no Supabase in the browser),
--   ficium-auth #31 (no /register), and 047/049/050 applied.
--
-- After Z4 nothing legitimate reaches the Portal DB through Supabase's public
-- API except ficium-auth, which uses the SERVICE key on auth_portal only.
-- This removes the browser-era access that made this week's takeover possible:
--   * Supabase-Auth self-registration policies (038) are no longer needed:
--     registration is one server-side transaction in portal-api.
--   * anon (no login at all) loses every remaining grant on Ficium schemas.
-- 'authenticated' keeps its grants: portal-api's tenant sessions SET ROLE to
-- it over a direct connection, and RLS still scopes every row.
--
-- DASHBOARD steps that SQL cannot do (Supabase project ficium-institution):
--   1. Authentication > Sign In / Providers: disable "Allow new users to sign up".
--   2. Project Settings > Data API > Exposed schemas: auth_portal only
--      (remove public, institution, portal_admin, marketplace, ...).
--   3. Database > Network restrictions: allow Railway's static egress IPs only
--      (needs Railway static IPs; Supabase dashboard/MCP keep working).
--   4. Edge function provision-institution-user: delete (stubbed 2026-10-21).

begin;

-- 1. Browser self-registration is gone: drop its policies and helpers.
drop policy if exists institutions_insert_self     on institution.institution;
drop policy if exists institution_members_insert_self on institution.member;
drop policy if exists member_insert_bootstrap_only on institution.member;
revoke insert on institution.institution, institution.member from authenticated;
drop function if exists institution.can_bootstrap_primary_admin(uuid);
drop function if exists institution.jwt_email();

-- 2. anon: nothing on any Ficium schema.
do $$
declare sch text;
begin
  foreach sch in array array['institution', 'marketplace', 'governance', 'audit', 'portal_admin',
                             'auth_portal', 'catalog', 'esign', 'integration', 'ops', 'public'] loop
    if exists (select 1 from pg_namespace where nspname = sch) then
      execute format('revoke all on all tables    in schema %I from anon', sch);
      execute format('revoke all on all sequences in schema %I from anon', sch);
      execute format('revoke all on all functions in schema %I from anon', sch);
      if sch <> 'public' then
        execute format('revoke usage on schema %I from anon', sch);
      end if;
      execute format('alter default privileges in schema %I revoke all on tables from anon', sch);
      execute format('alter default privileges in schema %I revoke all on functions from anon', sch);
    end if;
  end loop;
end $$;

do $verify$
declare n int;
begin
  select count(*) into n
    from information_schema.role_table_grants
   where grantee = 'anon'
     and table_schema in ('institution', 'marketplace', 'governance', 'audit', 'portal_admin',
                          'auth_portal', 'catalog', 'esign', 'integration', 'ops', 'public');
  if n <> 0 then raise exception '048: anon still has % table grant(s)', n; end if;
  if has_table_privilege('authenticated', 'institution.member', 'INSERT')
     or has_table_privilege('authenticated', 'institution.institution', 'INSERT') then
    raise exception '048: bank users can still insert institutions or members directly';
  end if;
end
$verify$;

commit;
