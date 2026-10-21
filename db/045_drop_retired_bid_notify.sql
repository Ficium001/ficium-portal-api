-- 045_drop_retired_bid_notify.sql
-- _retired_bid_notify_20261006 held two functions from the pre-contract bid
-- notifier, one of them making outbound HTTP calls (net.http_post). Nothing
-- depends on them and no trigger calls them; contract 1.10.0 (#90) replaced
-- the path. Dropped explicitly (RESTRICT) so anything unexpected fails loudly.
-- _retired_workflow_20261006 is deliberately NOT touched: it still has tables,
-- external dependencies and a trigger, and needs its own review.

begin;

do $$
declare f regprocedure;
begin
  for f in select p.oid::regprocedure from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = '_retired_bid_notify_20261006' loop
    execute format('drop function %s', f);
  end loop;
end $$;
drop schema if exists _retired_bid_notify_20261006 restrict;

do $verify$
begin
  if exists (select 1 from pg_namespace where nspname = '_retired_bid_notify_20261006') then
    raise exception '045: schema still present';
  end if;
  if exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
              where p.prosrc ilike '%net.http_post%'
                and n.nspname not in ('net', 'extensions', 'pg_catalog', 'cron')) then
    raise exception '045: a function on the Portal DB still makes outbound HTTP calls';
  end if;
end
$verify$;

commit;
