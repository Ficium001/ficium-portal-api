# ADR-002: Identity Migration — Supabase auth.users → ficium-auth auth_portal.auth_users

**Status:** Accepted  
**Date:** 2026-06-21

## Context

Ficium Portal uses two auth systems:
- **Supabase** (`auth.users`) — original identity store; `auth.uid()` returns its UUID
- **ficium-auth** (`auth_portal.auth_users`) — standalone RS256 JWT issuer on Railway; `sub` claim = its UUID

Every tenant table (`institution_members.auth_user_id`, `admin_users.auth_user_id`, etc.) was initially created with FKs pointing at `auth.users`. When ficium-auth was introduced, new JWTs carry a different UUID for the same person. `auth.uid()` reads `sub` from the JWT, which is now the ficium-auth UUID — but the DB rows still hold the Supabase UUID. RLS never matches; every policy silently returns nothing.

## Decision

**Option A — Identity backfill (chosen):** match users by email, update `auth_user_id` in all tenant tables to the ficium-auth UUID, drop old FK to `auth.users`, add new FK to `auth_portal.auth_users`.

Option B (dual-UUID column) and Option C (JWT claim enrichment) were rejected as more complex with higher ongoing maintenance cost.

## Migration

Run via `scripts/identity_backfill.py`:

```bash
# Always dry-run first
DATABASE_URL=... python scripts/identity_backfill.py --dry-run

# Apply only when dry-run exits 0 with no unmatched users
DATABASE_URL=... python scripts/identity_backfill.py --apply

# Verify
psql "$DATABASE_URL" -f scripts/verify_after_apply.sql
```

## Safety

- Dry-run by default; `--apply` blocked if any active user is unmatched
- Every write logged to `public._identity_migration_log` before execution
- Old FK dropped before UPDATEs (new UUIDs don't exist in `auth.users`)
- Idempotent — safe to re-run; already-correct rows are skipped

## Consequences

- `auth.uid()` will correctly resolve ficium-auth UUIDs → RLS enforced
- `current_member_ctx()`, `get_my_group()`, all RLS policies work correctly
- Supabase `auth.users` FK constraint removed from tenant tables
