-- 053_revoke_anon_definer_functions.sql
-- Security audit 2026-10-24. Postgres grants EXECUTE on new functions to PUBLIC,
-- so SECURITY DEFINER functions such as marketplace.accept_bid,
-- advance_pipeline_stage, relist_request and ingest_app_request were executable
-- by the anon role. Nothing legitimate uses anon on this database (portal-api
-- connects as postgres and runs tenant work as authenticated; the portal frontend
-- has no Supabase client), so this removes it everywhere.
-- authenticated / service_role keep exactly the access they had.
begin;

do $$
declare f record; n int := 0;
begin
  for f in
    select p.oid::regprocedure as sig,
           has_function_privilege('authenticated', p.oid, 'EXECUTE') as auth_had,
           has_function_privilege('service_role', p.oid, 'EXECUTE') as svc_had
      from pg_proc p join pg_namespace s on s.oid = p.pronamespace
     where p.prosecdef
       and s.nspname not in ('pg_catalog','information_schema','auth','storage','realtime','extensions',
                             'graphql','graphql_public','pgsodium','vault','net','cron',
                             'supabase_functions','pgbouncer','pg_toast')
       and has_function_privilege('anon', p.oid, 'EXECUTE')
  loop
    -- Keep authenticated exactly as it was (it may have had EXECUTE only via PUBLIC).
    if f.auth_had then
      execute format('grant execute on function %s to authenticated', f.sig);
    end if;
    if f.svc_had then
      execute format('grant execute on function %s to service_role', f.sig);
    end if;
    execute format('revoke execute on function %s from public, anon', f.sig);
    n := n + 1;
  end loop;
  raise notice '053: revoked anon/public EXECUTE on % definer functions', n;
end $$;

-- Future functions are guarded by tests/test_integration.py::
-- test_no_definer_function_executable_by_anon rather than by changing default
-- privileges (which would also remove EXECUTE from authenticated on new RLS
-- helper functions and break tenant queries).

do $$
declare n int;
begin
  select count(*) into n
    from pg_proc p join pg_namespace s on s.oid = p.pronamespace
   where p.prosecdef and has_function_privilege('anon', p.oid, 'EXECUTE')
     and s.nspname not in ('pg_catalog','information_schema','auth','storage','realtime','extensions',
                           'graphql','graphql_public','pgsodium','vault','net','cron',
                           'supabase_functions','pgbouncer');
  if n <> 0 then raise exception '053: % definer functions still executable by anon', n; end if;
end $$;

commit;
