# ADR-002 — ficium-auth becomes the sole identity source (Option A)

**Status:** Proposed
**Date:** 2026-06-15
**Owner:** Kishan Jeebun (kishan.jeebun@ficium.net)
**Depends on:** ADR-001 (portable Portal data layer)

---

## Context

ADR-001 made `ficium-portal-api` the transport between the browser and
Postgres, replacing PostgREST while keeping every existing RLS policy and
maker-checker function unchanged. That held because both sides — the JWT and
the SQL — were assumed to agree on what `auth.uid()` means.

They don't. Investigation (prompted by mcbadmin seeing "No modules assigned"
despite a fully-populated `institution_members` row) found two **independent**
identity spaces:

- **Supabase `auth.users`** — the original identity space. Every tenant table
  (`institution.institution_members.auth_user_id`,
  `portal_admin.admin_users.auth_user_id`) has a hard FK into it, and every
  RLS policy / `SECURITY DEFINER` function resolves identity by matching
  `auth.uid()` against these columns.
- **ficium-auth `auth_portal.auth_users`** — a separate table with its own
  `gen_random_uuid()` primary key, created independently when ficium-auth was
  built. Login no longer touches Supabase at all, so the JWT's `sub` claim is
  this new id — unrelated to the Supabase id sitting in every tenant table.

The result: `auth.uid()` (sourced from the verified JWT, via `request.jwt.claims`)
never matches `institution_members.auth_user_id` for an existing user. Every
join in `current_member_ctx()`, `get_my_group()`, and every RLS policy silently
returns nothing. Users with full, correct group/module assignments appear to
have none. This affects **every** existing institution and admin user, not
only mcbadmin — it has been masked until now because earlier testing happened
to occur before tenant tables were checked against the new login path.

## Decision

**Option A: migrate the identity, not the architecture.** ficium-auth's
`auth_portal.auth_users.id` becomes the one canonical identity used
everywhere. Every existing FK to `auth.users(id)` is repointed to the
matching ficium-auth user, matched by **email** (the one attribute guaranteed
present and unique in both systems — `auth_users.email` is `UNIQUE`,
`admin_users.email`/institution member emails are unique per the existing
schema).

This is the option consistent with the goal already established in ADR-001:
ficium-auth is becoming the platform's actual identity provider, not a proxy
in front of Supabase Auth. Rejected alternative: make ficium-auth issue
Supabase's id as `sub` (keeps Supabase load-bearing for identity forever —
exactly what ADR-001 was built to escape).

## Why this requires care

This is a write migration across live, regulated user data, executed via
`ficium-portal-api`'s service connection (not a casual script):

1. **Coverage must be schema-driven, not hand-listed.** This codebase has a
   precedent of tables created directly in Supabase, outside the migrations
   directory (`institution_members` itself, and `pending_actions`, per
   existing migration comments). A hardcoded list of "tables to fix" could
   miss one. The migration therefore **introspects `information_schema`** at
   run time for every column with a foreign key into `auth.users(id)` and
   processes whichever tables actually exist — see `db/migrations/002a_*.py`.

2. **Every email must resolve before any row is written.** A partial backfill
   — some members repointed, others left dangling — is worse than the current
   broken-but-consistent state (everyone currently fails the same way; a
   partial fix would make failures inconsistent and hard to diagnose). The
   migration runs in two phases: **dry-run** (report-only, default) and
   **apply** (only after a dry-run shows 100% resolution, explicit flag
   required).

3. **Reversible.** Before any UPDATE, the migration records the
   `(table, row_id, old_auth_user_id, new_auth_user_id)` mapping in an audit
   table (`_identity_migration_log`), so the change can be inspected or
   reversed without re-deriving it.

4. **Unmatched users are surfaced, not silently dropped.** A Supabase user
   with no corresponding ficium-auth account (never logged in since the
   ficium-auth cutover) cannot be resolved by email-join alone. The dry-run
   report lists these explicitly; they are out of scope for an automatic
   backfill and need an explicit decision (provision them in ficium-auth, or
   confirm they're inactive/offboarded) before `apply`.

5. **FK constraints must be dropped and re-added**, not just UPDATEd, because
   the column currently has `REFERENCES auth.users(id)` — pointing at a table
   that won't exist at all once Supabase is fully exited, and which provides
   no integrity guarantee against ficium-auth's table even today. The
   migration drops the old FK and adds a new one against
   `auth_portal.auth_users(id)` (or, for cross-database deployments where
   ficium-auth's table lives in a different physical database than the
   Portal schema, no DB-level FK — enforced at the application layer instead,
   noted explicitly per-deployment in the migration output).

## Consequences

Positive: `auth.uid()` resolves correctly everywhere; the identity-space
mismatch — the actual root cause of today's bug — is closed for every
existing and future user; ficium-auth is now genuinely the platform's auth
system, completing the move away from Supabase Auth.

Cost: a real, audited write operation against production data; any user who
has never logged in via ficium-auth since its cutover cannot be auto-migrated
and needs explicit handling; the FK semantics differ by deployment model
(same-database FK in SaaS-with-Supabase-Postgres; app-level enforcement once
ficium-auth and the Portal DB are physically separate, e.g. on-prem).

## Procedure

1. Run `scripts/identity_backfill.py --dry-run` against the target database.
   Produces a full report: total rows per table, resolved-by-email count,
   unmatched Supabase users, unmatched ficium-auth users.
2. Resolve every unmatched case by hand (provision missing ficium-auth
   accounts, or confirm-and-exclude offboarded users). Re-run dry-run until
   100% resolution for all *active* users.
3. Run `--apply`. Executes inside a single transaction per table; writes
   `_identity_migration_log` before each UPDATE; drops and recreates the FK
   at the end of each table's migration.
4. Verification block (below) confirms `current_member_ctx()` and
   `get_my_group()` now resolve for a known test user (mcbadmin).
5. Mark this ADR Accepted once verification passes in the target environment.

## Verification

```sql
-- Identity now matches:
SELECT im.id, im.auth_user_id, au.id AS ficium_auth_id, au.email
FROM institution.institution_members im
JOIN auth_portal.auth_users au ON au.id = im.auth_user_id
WHERE au.email = 'mcbadmin@mcb.mu';   -- old row: auth_user_id was the Supabase id

-- RLS-dependent resolution now works (run with claims set, as in db.py):
SELECT set_config('request.jwt.claims',
  '{"sub":"<ficium-auth id for mcbadmin>","role":"authenticated"}', true);
SELECT * FROM institution.current_member_ctx();   -- must return mcbadmin's row
SELECT portal_admin.get_my_group();                -- must return institution_admin group
```
