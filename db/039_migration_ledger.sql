-- 039_migration_ledger.sql
-- Portable migration ledger: which db/*.sql file ran, with which content.
-- Plain Postgres (no Supabase tables), so the same runner works on any
-- Postgres. scripts/migrate.py writes it; CI runs `migrate.py --check`,
-- which fails if a file in db/ is unapplied or was edited after applying.
-- Root cause it closes: 008/009/010 merged but never applied (Aug 2026),
-- and db/README.md silently missing 029-037.

create schema if not exists ops;
revoke all on schema ops from public;

create table if not exists ops.schema_migration (
  filename    text primary key check (filename ~ '^[0-9]{3}_[a-z0-9_]+\.sql$'),
  sha256      text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  status      text not null default 'applied' check (status in ('applied', 'baseline', 'skipped')),
  applied_at  timestamptz not null default now(),
  applied_by  text not null default current_user
);
comment on table ops.schema_migration is
  'One row per db/*.sql file. baseline = recorded as already live when the ledger was introduced.';

-- RLS on, deny-by-default for any client role; NOT forced, so a migration
-- role that owns the table without BYPASSRLS (plain Postgres) can still
-- read and write the ledger. Client roles get no grants on ops at all.
alter table ops.schema_migration enable row level security;
revoke all on ops.schema_migration from public;

-- Supabase client roles exist only on Supabase; stay portable elsewhere.
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on schema ops from %I', r);
      execute format('revoke all on ops.schema_migration from %I', r);
    end if;
  end loop;
end $$;
