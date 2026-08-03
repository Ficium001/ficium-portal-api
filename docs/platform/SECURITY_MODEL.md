# Ficium — Security Model

_Last verified 1 August 2026. Companion to `ARCHITECTURE.md`._

This document states the controls that exist and, equally, the ones that do not.
It is written to be handed to a penetration tester or a bank's third-party risk
team without further translation.

## 1. Trust boundaries

| Boundary | Crossed by | Control |
|---|---|---|
| Borrower browser → App DB | `supabase-js` with the user's JWT | RLS on every table |
| Borrower browser → Vercel functions | HTTPS | Supabase JWT verified server-side |
| Vercel functions → portal API | HTTPS, server-to-server | Shared secret, `/public/*` routes |
| Bank browser → portal API | HTTPS | RS256 JWT from `ficium-auth` |
| Portal API → either DB | Service session | **Bypasses RLS** — see §4 |
| App DB → portal API | `net.http_post` | Secret stored in Supabase vault |
| Portal API → institution endpoint | Outbound webhook | HMAC-SHA256, SSRF validation |

## 2. Data classification

| Class | Examples | Where it lives |
|---|---|---|
| **Restricted** | Borrower name, email, phone, address, DOB, ID number, KYC images, `auth.uid()` | App DB only. Crosses to the Portal DB only into `marketplace.bid_acceptance`, only on acceptance, only for the winner |
| **Confidential** | Dossier, income, liabilities, vault documents, net worth, FICO transcripts | App DB only. Never crosses |
| **Internal** | Bids, rates, pipelines, approvals, tenant configuration | Portal DB, tenant-scoped |
| **Public** | Product catalogue, currencies, countries, market rates | Either, unrestricted read |

The whole anonymity guarantee reduces to one sentence: **restricted data is not
in the Portal DB**, so no portal-side authorisation bug can leak it.

## 3. Authentication

**Borrower** — Supabase Auth, email + password, session in `localStorage`.
Password policy and rate limiting are Supabase defaults.

**Institution** — `ficium-auth`, username + password, RS256 JWT.
`auth_portal.auth_users` tracks `failed_attempts` and `locked_until` for
lockout, `must_change_password` for forced rotation, `password_changed_at` for
age, and optional TOTP MFA with hashed backup codes. Refresh tokens are stored
hashed in `auth_portal.auth_sessions` with device fingerprint, IP and user agent;
sessions can be revoked individually with a reason. Per-institution CIDR
allowlists exist in `auth_portal.ip_allowlist`.

The portal previously suffered an auth refresh loop; it is now guarded by a
singleton in-flight promise plus a circuit breaker.

**Machine** — `fic_live_<64 hex>` API keys stored as HMAC, never retrievable
after creation. Key creation is itself maker-checker: a key is inactive until a
second person approves it. Keys carry scopes, an optional expiry, and
`last_used_at` / `last_used_ip` for detection.

## 4. Authorisation

Three layers, all enforced server-side. Sidebar visibility is **not** a control.

1. **Entitlement** — `require_module(...)` on the route, against
   `institution.institution.modules`. Returns 403.
2. **Group permission** — `module_permissions[]` on the caller's group.
3. **Row-level security** — policies keyed on
   `institution.get_my_institution_id()` / `auth.uid()`.

### The service-session caveat

`ficium-portal-api` holds two connections and they enforce RLS differently.

**Portal DB (institution/marketplace/catalog/...)** — every request runs inside
`tenant_session()`: `set_config('request.jwt.claims', ...)` publishes the
verified claims, then `SET LOCAL ROLE authenticated` switches off the pooler's
`postgres`/`BYPASSRLS` connection role. Both are transaction-scoped. **RLS is
genuinely enforced on this path** — this is not a bypass. The role switch is
load-bearing: it was missing entirely from 2026-06-23 to 2026-06-30, during
which every RLS policy on every table was silently inert and isolation held only
by accident, wherever an endpoint's own `WHERE` clause happened to scope it.
`GET /marketplace/requests` had no such clause. The fix is a three-line change in
`core/db.py`, now load-bearing enough to carry a standing comment explaining why
it can never be removed.

**App DB (used only for the marketplace sync ingest and the `request_chat`
proxy)** — this connection carries no per-request claims and no role switch,
because there is no portal-side JWT to attach to it. **RLS genuinely does not
apply here.** Every invariant on this path has to be a trigger or a CHECK, not a
policy. `public.request_messages_enforce()` is the reference implementation: the
rules about who may send free text, and when, are enforced in a `BEFORE` trigger
precisely because RLS never fires for this connection. Any future rule on an
App-DB table reachable from the API must follow this pattern; any future Portal
DB rule reachable from the API can rely on RLS as normal.

### RLS coverage

**App DB** — RLS enabled on all 47 tables across `public`, `finance`, `fico`,
`admin`.

**Portal DB** — RLS enabled on 79 of 84 tables. Exceptions:

| Table | State | Assessment |
|---|---|---|
| `auth_portal.*` (7) | RLS on, zero policies | Correct — deny-all, service-role only |
| `marketplace.sync_state` | RLS off | Low risk; single-row cursor. Enable with no policy for tightness |
| `public._identity_migration_log` | RLS on, zero policies | Migration artefact, safe to drop |
| `admin.commission_event` | **RLS off** | **Review.** Per-deal revenue data |
| `admin.notification_log` | **RLS off** | **Review.** Lower sensitivity |

Both `admin.*` gaps are only exploitable if PostgREST exposes the `admin` schema.
Confirm the exposed-schema list before treating them as benign.

**Grants are separate from policies.** A new table gets neither automatically.
The `finance` schema outage was exactly this: correct policies, no `GRANT USAGE`
on the schema, and because the RPCs are `SECURITY INVOKER` every call 403'd
before RLS was evaluated. `ALTER DEFAULT PRIVILEGES` is now set for that schema.

## 5. Maker-checker

Applies to: institution approval and suspension, module entitlement changes,
group creation and permission changes, user provisioning and role changes, API
key creation, webhook changes, SLA changes, document template publication,
auto-bid rule activation, and — where licensed — bid submission.

`checker_id` must differ from `maker_id`. Actions expire after 7 days.
`payload_before` and `payload` capture the full diff. Execution is recorded
separately (`execution_status`, `executed_at`, `execution_error`) so an approved
action that failed to apply is distinguishable from one that never ran.

Approval chains add committee quorum, tie-break, delegation-of-authority routing
and per-stage SLAs. `approval_instance` freezes `template_version` and
`entity_snapshot` at route time, so amending a template cannot retroactively
change an in-flight decision.

## 6. Audit

| Store | Scope | Immutability |
|---|---|---|
| `public.audit_events` | Borrower actions | RLS, no UPDATE/DELETE policy |
| `admin.audit_events` | Ficium admin actions (App DB) | RLS |
| `audit.event` | All portal actions | `audit.block_mutation` trigger |
| `marketplace.bid_event` | Bid state transitions | `block_bid_event_mutation` trigger |
| `portal_admin.admin_audit_log` | Platform admin | RLS |
| `governance.action` | Maker-checker ledger | RLS |
| `auth_portal.auth_audit_events` | Login, lockout, MFA, session events | Service-role only |
| `identity.login_event` | Login outcomes with geo | RLS |
| `client_vault_access_log` | Every vault document view/download | RLS |

Captured per event: actor id, type, role, email, IP, user agent, resource type
and id, before/after state, outcome, and a correlation `request_id`.

E-signature envelopes carry their own **hash-chained** event trail, so a tampered
intermediate event is detectable.

## 7. Application-layer controls

- **CSP** — `script-src 'self'`, no `unsafe-eval`. (An `eval` console error
  observed in the field came from a browser extension's `ContentScript.js`, not
  app code.)
- **SSRF** — `app/core/ssrf.py` validates webhook endpoint URLs at registration.
- **Rate limiting** — `app/core/ratelimit.py`; pre-auth NIC scanning is
  IP-rate-limited via `kyc_scan_attempts` with a hashed IP.
- **Response headers** — `app/core/response_headers.py`.
- **Role constants** — `INSTITUTION_ADMIN_ROLES` centralised in
  `app/core/roles.py` after five API files were found missing the guard.
- **Idempotency** — `marketplace.request.idempotency_key` and
  `marketplace.bid.idempotency_key` prevent double-write on retry.
- **Observability** — `app/core/observability.py`.

## 8. Secrets and key management

Nothing sensitive is stored in plaintext: `password_hash`, `token_hash`,
`code_hash`, `key_hmac`, `secret_hash`, `refresh_token_hash`, `checksum_sha256`.
The sync secret and callback URL live in the Supabase vault, not in application
configuration.

External credentials in use: AWS IAM `AWSficium_Rekognition` (`ap-south-1`) for
KYC; Google Cloud project `storied-toolbox-498217-n2`; Anthropic API for Claude
Vision, FICO and the request builder; Finnhub and CoinGecko for market pricing.

## 9. Supply chain

All three repositories run: Dependabot, TruffleHog secret scanning, Semgrep SAST,
and typecheck/lint/build gates.

Both frontends replace a bare `npm audit --audit-level=high` with
`scripts/audit-gate.mjs` — an allowlist keyed on advisory ID, each entry carrying
a written rationale and a `reviewBy` date. An unlisted high or critical finding
fails the build, **and so does an expired entry**, so an exception cannot silently
become permanent.

One live exception: the react-router advisory (GHSA-qwww-vcr4-c8h2) covers
7.12.0–8.2.0 with no fixed version published — npm's only offer is a downgrade to
7.11.0, which is both older and breaking. The vulnerable path is RSC mode; both
apps are client-rendered Vite SPAs on `createBrowserRouter`/`RouterProvider` with
no RSC entrypoint, so it is not reachable. Current `reviewBy` is 2026-10-31.

## 10. Open items

Stated plainly rather than buried.

| Item | Status |
|---|---|
| Cobalt penetration test | **Not booked.** ~4-week lead time; the hardest external blocker to go-live |
| `admin.commission_event` / `notification_log` RLS | Not enabled — decide and document |
| Three overlapping admin identity models | `portal_admin`, `admin`, `identity` all live |
| RBAC gaps | `institution_checker` and `bank_officer_approver` lack `inst:esign` / `inst:approvals`; an open design call, not an oversight |
| `MODULE_ENTITLEMENT_KEY` inconsistency | Cannot be normalised frontend-only without revoking live entitlements |
| Cross-DB referential integrity | None by design; no reconciliation job exists |
| GitHub PAT rotation | Tracked as a pre-go-live item |
