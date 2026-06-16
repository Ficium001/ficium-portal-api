# Identity backfill — ADR-002

Fixes the root cause behind "No modules assigned to your account" for
existing users (mcbadmin and others): ficium-auth issues tokens keyed to its
own `auth_portal.auth_users.id`, but every tenant table still has
`auth_user_id` pointing at Supabase's `auth.users.id` — two different UUIDs
for the same person. `auth.uid()` never matches, so RLS and
`current_member_ctx()`/`get_my_group()` silently resolve to nothing.

Full design and rationale: [`ADR-002-identity-migration.md`](./ADR-002-identity-migration.md).

## Tested

The script was validated against a synthetic schema mirroring production
exactly (`test/synthetic_schema.sql`) before being run anywhere real — five
cases: a normal match, a second normal match, an admin_users match, a user
with no ficium-auth account (must be reported, not silently skipped), and a
mixed-case email (must still resolve). All five behaved correctly; one real
bug (FK had to be dropped *before* the row updates, not after) was caught and
fixed during this testing, never against real data.

## Run it

```bash
export DATABASE_URL=postgresql://...   # same DSN ficium-portal-api uses

# 1. Dry run — read-only, reports coverage, exits non-zero if incomplete.
python scripts/identity_backfill.py --dry-run

# 2. If any users are unmatched, provision them in ficium-auth (or confirm
#    they're offboarded/out of scope) and re-run dry-run until exit code 0.

# 3. Apply — refuses to run unless the dry-run is fully resolved.
python scripts/identity_backfill.py --apply

# 4. Verify — see scripts/verify_after_apply.sql for the full checklist,
#    including mcbadmin's specific case.
psql "$DATABASE_URL" -f scripts/verify_after_apply.sql
```

## What it actually does

Discovers every column with a foreign key into `auth.users(id)` via
`information_schema` (not a hardcoded table list — this codebase has tables
created directly in Supabase outside the migrations directory, so a
hardcoded list can't be trusted to be complete). For each one:

1. Joins the affected rows to `auth.users` to get the email.
2. Looks up that email in `auth_portal.auth_users` (ficium-auth's table).
3. Writes an audit row to `public._identity_migration_log` before each
   change.
4. Drops the old FK (it must come first — the old FK rejects writes of a
   ficium-auth id while still pointing at `auth.users`), updates the column,
   then adds a new FK pointing at `auth_portal.auth_users`.

Idempotent: once a table's FK points at `auth_portal.auth_users`, the
discovery query no longer finds it, so re-running the script is a safe no-op.

## On-premises / split-database note

This assumes `auth_portal.auth_users` is reachable from the same connection
as the tenant tables (true for SaaS today — both live in the Supabase
Postgres). Where ficium-auth's database is physically separate from the
Portal database (a real possibility for on-prem), the FK becomes
app-enforced rather than DB-enforced; this script's email-matching logic
still applies, but the final `ALTER TABLE ... ADD CONSTRAINT` step needs
adjusting per ADR-002's note on deployment-specific FK semantics.
