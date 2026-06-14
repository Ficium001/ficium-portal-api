# ADR-001 — Portable Portal data layer (off-Supabase, MRU-residency capable)

**Status:** Proposed
**Date:** 2026-06-13
**Owner:** Kishan Jeebun (kishan.jeebun@ficium.net)
**Supersedes:** the browser→Supabase token-injection approach (ficium-portal PR #3 commit `84aa345`)

---

## Context

Ficium Portal must run in three deployment models — SaaS, client cloud, and
on-premises — and may need customer data to reside in Mauritius and to leave
Supabase entirely. The original design had the browser call Supabase PostgREST
directly, with Supabase RLS enforcing tenant isolation off a Supabase-issued
JWT.

Replacing Supabase Auth with ficium-auth broke that path. Investigation
established, in order:

1. ficium-auth signs the session JWT (confirmed `alg: HS256`, correct
   `authenticated` claims).
2. Supabase REST rejects it: `PGRST301 — None of the keys was able to decode
   the JWT`. The project migrated to **asymmetric JWT signing keys** (current
   key ECC P-256); PostgREST verifies only against that key and no longer
   honours the legacy HS256 shared secret for the Data API.
3. Supabase cannot be made to trust ficium-auth tokens: the ECC private key is
   not exportable ("View key details" only), and Third-Party Auth offers only
   branded providers (Firebase, Clerk, WorkOS, Auth0, Cognito) — no generic
   JWKS issuer.

More fundamentally, **any** design where Supabase verifies the token is
incompatible with on-premises deployments (no Supabase present) and with
MRU data-residency requirements.

## Key finding

The Portal's security and business logic already lives in Postgres and is
**portable as-is**:

- Tenant isolation: RLS `ENABLE` + `FORCE` on tenant tables, every policy keyed
  on `institution_id` via `institution.current_member_ctx()`.
- Maker-checker / dual-control: `pending_actions` + `submit_for_approval` /
  `approve_action` / `reject_action` (SECURITY DEFINER), including the
  sole-admin self-approval override.
- Identity resolution: `current_member_ctx()` → `auth.uid()` →
  `current_setting('request.jwt.claims')->>'sub'`.

Only the **transport** is Supabase-specific: PostgREST injecting
`request.jwt.claims`, the `auth.*` helpers, Edge Functions (user provisioning),
and Storage.

## Decision

Introduce **`ficium-portal-api`**, a thin FastAPI data service that replaces
PostgREST. For every request it verifies the ficium-auth RS256 JWT, opens a
transaction, runs `SET LOCAL ROLE authenticated` and
`set_config('request.jwt.claims', <verified claims>, true)`, then executes the
query/RPC. All existing RLS policies and SECURITY DEFINER functions run
**unchanged**.

Portability is delivered by `db/000_auth_shim.sql`, which recreates the
`auth.uid()/auth.jwt()/auth.role()` contract and the `authenticated`/`anon`
roles on any non-Supabase Postgres. The connection string is the only thing
that varies between deployment models.

ficium-auth moves to **RS256** with its own keypair (drop `SUPABASE_JWT_SECRET`)
and publishes a JWKS endpoint; `ficium-portal-api` verifies against it. No
shared secrets, no key export, clean issuer separation.

The frontend drops `@supabase/supabase-js` and calls `ficium-portal-api`
through a typed client. This removes the last Supabase lock-in from the Portal.

### Rejected alternatives

- **Make Supabase trust ficium-auth tokens** — impossible (no generic JWKS
  provider; ECC key not exportable) and fatal for on-prem / residency.
- **Mimic a branded Third-Party Auth provider** — brittle, undocumented, unfit
  for a regulated platform.
- **Rewrite RLS + dual-control in Python** — discards tested, regulated SQL;
  high risk; rejected.

## Consequences

Positive: one identity path in all three models; tenant isolation enforced by
Postgres (defence in depth), not app code; DB relocatable to MRU/on-prem by
config; Supabase becomes optional.

Cost: `ficium-portal-api` must cover 18 tables, 13 RPCs, document storage, and
user provisioning; the frontend data layer is rewired off supabase-js; user
provisioning (previously the `provision-institution-user` Edge Function, which
needed service-role `auth.admin`) moves into ficium-auth, which now owns users.

Open decision (business/regulatory, owner's call): in SaaS, keep the existing
Supabase Postgres as the backing store (server-side direct connection,
relocatable later) **or** stand up an MRU-hosted Postgres now. On-prem and
client-cloud satisfy residency by construction.

## Build sequence (each stage production-grade, no throwaway scaffolding)

1. **Portable core** — `db/000_auth_shim.sql` + `app/core/db.py`
   (tenant_session claim bridge) + JWT verification. *(this PR)*
2. ficium-auth → RS256 + JWKS endpoint; retire `SUPABASE_JWT_SECRET`.
3. First vertical slice end-to-end: `PortalRoute` institution status →
   `GET /institutions/me`. Unblocks login under the new architecture.
4. Institution module: members, groups, pending_actions + the three
   maker-checker RPCs, products, webhooks, SLA/product config, audit.
5. Admin module: dual-control RPCs, sessions, audit, institutions list.
6. Document storage → S3-compatible abstraction (MinIO on-prem / MRU object
   store / Supabase Storage in SaaS).
7. User provisioning → ficium-auth endpoint (replaces the Edge Function).
8. Frontend: typed `ficium-portal-api` client; remove `@supabase/supabase-js`.
9. `ficium-infra` manifests for all three models; CI/CD pipelines
   (lint, type-check, test, build, deploy) per repo.
