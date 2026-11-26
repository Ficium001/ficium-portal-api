-- =============================================================================
-- 075 — monthly partitions can never be missing, and carry RLS; duplicate
--       indexes dropped. From the database audit, 2026-11-25/26.
--
-- A. entitlements.usage_event and autobid.execution are partitioned by month,
--    but nothing scheduled ensure_*_partitions(): only Aug–Oct 2026 existed, so
--    the first metering write (app/core/entitlements.py) or autobid execution
--    after October would fail with "no partition of relation found". Both
--    tables are empty, so nothing was lost. Now:
--      - a DEFAULT partition on each, so a write never fails;
--      - the current month and the next two are created now, and daily by
--        pg_cron (where pg_cron exists; a plain-Postgres cell calls
--        ensure_*_partitions() from its own scheduler);
--      - every partition, new or old, has RLS enabled and forced. The parents
--        already do; this closes direct access to a partition by name.
--    The DEFAULT partition is created ahead of time only to catch writes when
--    the scheduler has been down for months; ensure_*_partition() refuses a
--    month whose rows already sit in DEFAULT (move them first).
-- B. The four ensure_* functions are SECURITY DEFINER and ran DDL as postgres
--    for any signed-in member. EXECUTE now: service_role only.
-- C. 11 duplicate indexes dropped: each has an identical twin, so the planner
--    uses the other. Kept: the one backing a constraint, else the
--    conventionally named one. auth_portal's 4 pairs belong to ficium-auth
--    (its migration 003).
--
-- Date-named partitions are data layout made by a function, not schema:
-- scripts/schema_baseline.py leaves them out of both dumps, so the daily
-- rebuild-and-compare does not drift every month.
-- Guarded: runs in order on a fresh cell.
-- =============================================================================

-- ── A + B: partition helpers ────────────────────────────────────────────────
create or replace function entitlements.ensure_usage_partition(p_month date)
returns void language plpgsql security definer
set search_path to 'entitlements', 'pg_temp' as $function$
declare
  part_start date := date_trunc('month', p_month)::date;
  part_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
  part_name  text := format('usage_event_%s', to_char(part_start, 'YYYY_MM'));
begin
  if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                  where n.nspname = 'entitlements' and c.relname = part_name) then
    execute format('create table entitlements.%I partition of entitlements.usage_event
                      for values from (%L) to (%L)', part_name, part_start, part_end);
    execute format('alter table entitlements.%I enable row level security', part_name);
    execute format('alter table entitlements.%I force row level security', part_name);
  end if;
end $function$;

create or replace function autobid.ensure_execution_partition(p_month date)
returns void language plpgsql security definer
set search_path to 'autobid', 'pg_temp' as $function$
declare
  part_start date := date_trunc('month', p_month)::date;
  part_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
  part_name  text := format('execution_%s', to_char(part_start, 'YYYY_MM'));
begin
  if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                  where n.nspname = 'autobid' and c.relname = part_name) then
    execute format('create table autobid.%I partition of autobid.execution
                      for values from (%L) to (%L)', part_name, part_start, part_end);
    execute format('alter table autobid.%I enable row level security', part_name);
    execute format('alter table autobid.%I force row level security', part_name);
  end if;
end $function$;

revoke all on function entitlements.ensure_usage_partition(date),
                       entitlements.ensure_usage_partitions(),
                       autobid.ensure_execution_partition(date),
                       autobid.ensure_execution_partitions()
  from public, anon, authenticated;
grant execute on function entitlements.ensure_usage_partition(date),
                          entitlements.ensure_usage_partitions(),
                          autobid.ensure_execution_partition(date),
                          autobid.ensure_execution_partitions()
  to service_role;

-- DEFAULT partitions: a write never fails, even before the first scheduled run.
create table if not exists entitlements.usage_event_default
  partition of entitlements.usage_event default;
create table if not exists autobid.execution_default
  partition of autobid.execution default;

-- RLS on every partition that exists now.
do $p$
declare r record;
begin
  for r in select c.oid::regclass as part
             from pg_inherits h join pg_class c on c.oid = h.inhrelid
            where h.inhparent in ('entitlements.usage_event'::regclass, 'autobid.execution'::regclass)
  loop
    execute format('alter table %s enable row level security', r.part);
    execute format('alter table %s force row level security', r.part);
  end loop;
end $p$;

select entitlements.ensure_usage_partitions();
select autobid.ensure_execution_partitions();

-- Daily, where pg_cron exists (Supabase). Idempotent; creates 2 months ahead.
do $c$
begin
  if to_regclass('cron.job') is not null then
    perform cron.unschedule(jobid) from cron.job where jobname = 'ensure-monthly-partitions';
    perform cron.schedule('ensure-monthly-partitions', '10 0 * * *',
      'select entitlements.ensure_usage_partitions(); select autobid.ensure_execution_partitions();');
  end if;
end $c$;

-- ── C: duplicate indexes (each has an identical twin that stays) ────────────
drop index if exists governance.idx_governance_expiry;            -- = action_pending_expiry_idx
drop index if exists institution.idx_dg_entity;                   -- = doc_generation_entity_idx
drop index if exists institution.idx_dg_inst;                     -- = doc_generation_institution_idx
drop index if exists institution.idx_dt_inst;                     -- = doc_template_institution_idx
drop index if exists institution.idx_dtv_tmpl;                    -- = doc_template_version_template_idx
drop index if exists institution.idx_inst_groups_institution;     -- = groups_slug_unique_per_institution (unique)
drop index if exists institution.ix_members_institution_id;       -- = idx_member_institution
drop index if exists institution.idx_prod_config_inst;            -- = institution_product_config_institution_id_product_id_key (unique)
drop index if exists portal_admin.idx_admin_users_auth;           -- = admin_users_auth_user_id_key (unique)
drop index if exists portal_admin.idx_admin_users_email;          -- = admin_users_email_key (unique)
drop index if exists portal_admin.idx_user_groups_slug;           -- = user_groups_slug_key (unique)
