# Ficium — Platform Architecture

_Last verified 1 August 2026 against `ficium`, `ficium-portal`, `ficium-portal-api` at `main`, and both live Supabase projects._

## 1. What Ficium is

Ficium is a **reverse-lending marketplace**. Instead of a borrower applying to
banks one at a time, the borrower posts a single anonymised financing request and
licensed institutions bid competitively for it. The borrower picks a winner;
only at that moment does the winning institution learn who they are.

The same mechanism carries investment products — the borrower posts a savings or
investment objective and institutions bid with rates and terms.

Everything below follows from two commitments:

1. **Anonymity until acceptance.** An institution must not be able to identify a
   borrower, or link two requests to the same person, before that borrower
   accepts its bid.
2. **Bank-grade controls.** Maker-checker on anything consequential, append-only
   audit, per-tenant isolation enforced in the database rather than in
   application code.

## 2. System map

```
                            ┌───────────────────────────┐
   Borrower (mobile-first)  │  ficium                   │  React/TS/Vite → Vercel
                            │  + 12 serverless functions│
                            └─────────┬─────────────────┘
                                      │ supabase-js (RLS)      ┌──────────────┐
                                      ├───────────────────────►│   App DB     │
                                      │                        │ Supabase PG  │
                                      │ /api/* → portal-api    │ 47 tables    │
                                      │                        └──────┬───────┘
                                      ▼                               │
                            ┌───────────────────────────┐             │ pg_cron +
   Bank officer             │  ficium-portal-api        │             │ trigger kick
                            │  FastAPI → Railway        │◄────────────┘ (net.http_post)
                            │  163 endpoints            │
                            └─────────┬─────────────────┘
                                      │ service session        ┌──────────────┐
   ┌───────────────────────┐          ├───────────────────────►│  Portal DB   │
   │  ficium-portal        │──────────┘                        │ Supabase PG  │
   │  React/TS/Vite→Vercel │  JWT (RS256)                      │ 84 tables    │
   └───────────┬───────────┘                                   └──────────────┘
               │                      ┌───────────────────────┐
               └─────────────────────►│  ficium-auth          │ FastAPI → Railway
                  username/password   │  RS256 JWT issuer     │
                                      └───────────────────────┘
                                      ┌───────────────────────┐
                                      │  ficium-rating-engine │ Python → Railway
                                      └───────────────────────┘
```

`ficium-shared` holds cross-repo types; `ficiumdev` is the internal agent
framework and is not part of the runtime.

## 3. The two-database split

This is the defining architectural decision and the source of most of the
platform's complexity, so it is worth being explicit about why it exists.

The borrower's data — identity, KYC, financial dossier, vault documents,
net worth, couple links — lives in the **App DB**. The institution's world —
tenants, members, bids, pipelines, approvals — lives in the **Portal DB**. There
is no connection between them at the database level. `ficium-portal-api` is the
only process that holds credentials to both, and it never joins across them in a
single query.

**What this buys.** A compromise of the institution portal cannot reach borrower
PII, because the credentials in that process only address the Portal DB. Phase-1
anonymity is a property of what was copied, not of a `WHERE` clause someone might
forget.

**What it costs.** Every borrower fact an institution needs has to be explicitly
projected across the boundary, and there is no referential integrity across it.
`marketplace.request.consumer_id` points at an App DB client that Postgres cannot
see. Both databases can be independently correct and jointly inconsistent.

### 3.1 The sync

App DB → Portal DB, one direction, never the reverse.

```
public.requests row changes
  └─ trigger trg_marketplace_sync
       └─ marketplace_sync.on_request_change()
            └─ marketplace_sync.dispatch()
                 └─ net.http_post → POST /marketplace/sync-requests
                                      (URL + shared secret in Supabase vault)
                                        │
                                        ▼
                     ficium-portal-api reads marketplace.sync_state,
                     pulls the next batch from the App DB,
                     calls marketplace.ingest_app_request() per row,
                     advances the cursor
```

Two properties matter:

**It is a kick, not a payload.** The trigger says only "something changed"; the
endpoint decides what to pull. A dropped notification therefore costs latency,
not data — the next kick or the pg_cron sweep picks it up. This is deliberate and
should not be "optimised" into a payload-carrying webhook without also solving
ordering and replay.

**The trigger is not the only trigger of a sync.** A `pg_cron` job,
`marketplace-sync-sweep` (`*/5 * * * *`), calls `marketplace_sync.dispatch()`
independently of any row change, as a safety net against a missed or failed HTTP
kick. Both paths converge on the same idempotent cursor logic below, so running
one after the other is harmless.

**Requests are anonymised with a salted, deterministic hash, not a random
token.** `marketplace.ingest_app_request()` computes
`v_anon_id := md5(consumer_id::text || ':ficium-anon-v1:')::uuid`. The same
borrower always maps to the same anon id — needed so an institution can
recognise a repeat applicant across requests without ever seeing their real
identity — but the hash cannot be reversed to the App DB client id without the
salt.

**The cursor is composite.** `marketplace.sync_state` stores
`(last_updated_at, last_id)`, not a bare timestamp. A bulk `UPDATE` stamps many
rows with an identical `now()`; if a batch boundary lands inside such a tie group
a timestamp-only watermark skips the remainder of the group permanently. This was
verified against a real 3-row tie group in the App DB where the naive version
lost two rows. The cursor advances only across a *contiguous* run of successes,
so a row that fails to ingest is retried rather than skipped.

`?full_resync=true` resets to epoch for recovery. The endpoint returns
`watermark`, `watermark_id` and `more_pending` so a saturated batch is visible
rather than silent.

**Prerequisite worth remembering:** `public.requests.updated_at` had a `DEFAULT`
but no trigger until recently, so ordinary updates never bumped it. Any
`updated_at`-keyed sync built before `touch_updated_at()` existed was silently
lossy. If a new table is ever added to the sync, check its trigger first.

### 3.2 The ingest allowlist

`marketplace.ingest_app_request()` copies a **hardcoded allowlist** of phase-1
keys into `marketplace.request.metadata`. Anything not on the list is dropped
without error. This is the failure mode that made investment-side bid PDFs thin
for months: the app collected risk appetite, horizon, liquidity and style; all
four layers above were fixed; and the data still did not arrive because the
allowlist had not been extended.

**Any new phase-1 field requires a Portal DB migration to that function.** There
is no runtime signal when a field is dropped.

## 4. Authentication

Two entirely separate systems that never interact.

**Borrower (`ficium`)** — Supabase Auth, email + password, session in
`localStorage` under `ficium-auth`. `AuthContext` initialises from the session,
resolves role via the `get_my_role()` RPC, and prefetches dashboard data before
the router renders.

Use `getCachedUser()` from `shared/lib/supabase.ts` for identity, not
`supabase.auth.getUser()`. The latter issues a `GET /auth/v1/user` round-trip on
**every** call; 22 call sites were using it purely to obtain `user.id` for a
query that RLS already scopes. Reserve the real `getUser()` for server-verified
identity ahead of a privileged, non-RLS-guarded action.

**Institution (`ficium-portal`)** — `ficium-auth` issues RS256 JWTs against
`auth_portal.auth_users`, keyed on username rather than email, with a forced
password-change flow on first login. `ficium-portal-api` verifies the signature
and reads `institution_id` and role claims from it. The portal has only two
`supabase.auth.getUser()` call sites, both on cold login/registration paths.

**Service-to-service** — `ficium-portal-api` holds two different kinds of
connection and they enforce RLS differently. For the **Portal DB**, every
request runs inside `tenant_session()`, which does
`set_config('request.jwt.claims', ...)` and then `SET LOCAL ROLE authenticated`
— both transaction-scoped. The role switch is not cosmetic: the pooler connects
as `postgres` (`BYPASSRLS`), so without it every RLS policy on every table is
silently inert regardless of how correct the policy itself is. This exact
failure happened in production on 2026-06-30 — the role switch was missing
entirely, isolation held only where an endpoint's own `WHERE` clause happened to
scope it, and `GET /marketplace/requests` had none. It is fixed and now carries
a standing comment in `core/db.py` so it cannot regress silently again.

For the **App DB** — used only for the marketplace sync ingest and the
`request_chat` proxy — the connection is a bare service credential with no
per-request claims and no role switch, because there is no portal-side JWT to
attach. RLS genuinely does not apply on that path. `public.request_messages_enforce()`
is the reference example of the correct response: the structured-vs-free-text
chat rules are enforced in a `BEFORE` trigger precisely because RLS would not
fire for that connection. Any future constraint on an App-DB table reachable
from the API needs the same treatment; any future Portal DB constraint reachable
from the API can rely on RLS.

## 5. Authorisation

Three independent layers, all of which must pass.

| Layer | Mechanism | Stored in |
|---|---|---|
| **Entitlement** — is this module licensed to the tenant? | `require_module(...)` dependency on the route | `institution.institution.modules` (jsonb) |
| **Permission** — is this module granted to the user's group? | `module_permissions[]` checked in `PortalShell` and on the API | `institution.group` / `portal_admin.user_groups` |
| **Row scope** — may this row be read? | RLS via `institution.get_my_institution_id()` | Policy per table |

A module hidden in the sidebar is not a security control; the API check is. But
the reverse gap is real and has bitten: `NAV_SECTIONS` in `PortalShell.tsx` was a
hardcoded key whitelist with no catch-all, so a module that was licensed *and*
permitted still never rendered. It now has a fallback "Other" bucket.

**Trap.** `MODULE_ENTITLEMENT_KEY`'s values are inconsistent — `require_module("pipeline")`
vs `("inst:doctemplates")` vs `("AUTOBID")`. That inconsistency mirrors genuinely
inconsistent backend keys. Normalising the frontend map alone silently revokes
live entitlements; it needs a backend change plus a migration of
`institution.modules` values, in that order.

## 6. Request lifecycle

```
 draft ──► open ──────────────► accepted ──► pipeline ──► completed
             │  bid window        (Phase 2       │
             │  (default 4h)       reveal)       └─► withdrawn / declined
             ├──► expired  (expire_overdue_requests, hourly pg_cron)
             ├──► rejected (all invited institutions declined)
             └──► awaiting_consent  (multi-participant requests)
```

Portal-side statuses differ: `open | bidding | accepted | cancelled | expired`.
The mapping lives inside `ingest_app_request()`.

Bid window defaults to 4 hours from `catalog.product_sla.bid_window_minutes`,
overridable per tenant via `institution.institution_sla_config` and per product
via `institution.product_config`. `marketplace.guard_bid_window()` rejects late
bids; `close_expired_windows()` sweeps.

On acceptance, `marketplace.accept_bid()` runs one transaction that sets
`winning_bid_id`, writes `marketplace.bid_acceptance` (the Phase-2 reveal), and
calls `create_pipeline_from_acceptance()` to instantiate the loan pipeline from
the institution's template. Losing bids are marked rejected and their chat
threads frozen.

## 7. Approval and control

Two distinct systems that are often confused:

- **Dual control** (`institution.pending_actions`, `governance.action`) — generic
  four-eyes on internal changes: creating a group, provisioning a user, rotating
  an API key. Maker submits, a different checker approves, then the action
  executes. `checker_id` must differ from `maker_id`.
- **Approval chains** (`inst:approvals`) — configurable multi-stage approval for
  business entities: bids, offer letters, mandates. Committees with quorum rules,
  delegation-of-authority routing by amount/product/risk, per-stage SLAs with
  notify-or-escalate, and a checklist per stage. `approval_instance` freezes
  `template_version` and `entity_snapshot` at route time so a later template edit
  cannot rewrite an in-flight decision.

`/approvals` and `/dual-control` are **not** duplicates. They partition
`pending_actions` — `bid.*` categories versus everything else — via a server-side
`?scope=bids|internal` parameter.

## 8. Document generation and e-signature

```
doc_template ──► doc_template_version ──► (maker-checker publish)
                                              │
loan_pipeline stage ──► generate ─────────────┘
                          │  docxtpl merge, {{ field }} tags
                          ▼
                    doc_generation ──► .docx + .pdf (headless LibreOffice)
                          │              in the institution-docs bucket
                          ▼
                    esign envelope ──► borrower ceremony (OTP)
                          │
                          ▼
                    sealed PDF + hash-chain audit trail
```

`data_snapshot` on `doc_generation` freezes the merge inputs, so regenerating a
document later reproduces it byte-for-byte even if the deal has moved on.

Note the **bid PDF is a different thing entirely** — `buildPDFHtml` in
`RequestDetailDrawer.tsx` is client-side HTML plus `window.print()`. It does not
touch docxtpl or the storage bucket. Changing one does not change the other.

## 9. Request chat

`public.request_messages` lives **only in the App DB**. The portal cannot reach
it directly; `ficium-portal-api` proxies via an App DB service session
(`app/api/request_chat.py`).

- One thread per `(request_id, institution_id)` — not per bid, so a withdrawn and
  resubmitted bid keeps its conversation.
- Pre-acceptance is **structured only**: both sides pick from
  `request_message_template` (19 entries — 8 borrower questions, 11 lender
  answers). There is no free-text field at all. Free text unlocks only for the
  winning lender after acceptance, gated by `can_send_free_text` from the API.
- 6 of the 11 active institution templates carry a non-empty `params_schema`
  across five types (`int`, `decimal`, `enum`, `string_list`,
  `label_amount_list`). The API substitutes params server-side and leaves
  unmatched placeholders **verbatim** — a composer that sent only a template code
  would deliver literal `{days}` text to borrowers. The portal's
  `TemplateParamFields` blocks send until every placeholder is filled.
- `_mask()` omits `sender_id` on the institution read path. That field is the
  borrower's real `auth.uid()` and is stable across all their requests.

## 10. Deployment

| Component | Platform | Notes |
|---|---|---|
| `ficium` | Vercel | Native Git integration. Hobby-plan ceiling of 12 serverless functions is currently exactly met — adding a 13th root-level `api/*.ts` breaks the deploy. |
| `ficium-portal` | Vercel | Public repo (private drew from the paid Actions quota and blocked CI). |
| `ficium-portal-api` | Railway | Docker, Python 3.14-slim. Cold-start latency is the platform's main perf issue, not bundle size. |
| `ficium-auth` | Railway | |
| `ficium-rating-engine` | Railway | |
| Both databases | Supabase | Migrations via `apply_migration`, never `execute_sql`. |

CI on all three repos: typecheck/lint/build, TruffleHog secret scan, Semgrep
SAST, Dependabot. `scripts/audit-gate.mjs` replaces a bare `npm audit` on both
frontends — an allowlist keyed on advisory ID, each entry carrying a written
rationale and a `reviewBy` date. Unlisted high/critical findings fail; an expired
entry also fails, so an exception cannot quietly become permanent.

## 11. Frontend conventions

Every feature is a self-contained module with its own `pages/`, `components/`,
`hooks/`, `api/`, `types/`. No cross-module imports except through `shared/`.
Each portal module has a key in `institution.modules`.

Server state is TanStack Query v5. Query keys are declared in a `*QueryKeys`
object in the feature's hook file, `staleTime` is always explicit, and mutations
invalidate the relevant key namespace. Polling uses the visibility-aware helpers
in `shared/lib/polling.ts` — bank officers leave the portal open in background
tabs, and nine queries were previously polling at full rate regardless.

**Verification gotcha:** `npx tsc --noEmit -p .` can false-pass on a stale
`.tsbuildinfo` left by a prior `tsc -b`. Always use `tsc -b --force` when
checking locally. CI is unaffected — clean checkout, and `.tsbuildinfo` is
gitignored.

## 12. Known limits

Recorded honestly rather than aspirationally.

- **The sync does not scale as designed.** A cross-database pull-sync with a
  kick is right for current volume. At real volume it wants to be
  payload-carrying/event-driven or logical replication. The keyset cursor makes
  it *correct*, not *fast*.
- **RLS evaluation cost** on Supabase shared infrastructure shows up well before
  a million users, on every table.
- **No referential integrity across the database boundary.** Nothing prevents a
  `marketplace.request` from outliving the App DB client it points at.
- **Three overlapping admin identity models** (`portal_admin`, `admin`,
  `identity`) are live simultaneously. See `DATA_DICTIONARY.md` § Known schema debt.
- Current scale is 3 users / 12 requests / 28 MB. The right sequence is fix
  correctness, then get users, then scale — in that order.
