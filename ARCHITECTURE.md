# ficium-portal-api — Architecture

_Last updated: 1 July 2026_

The portable data API for the Ficium Portal and the primary integration point for institution bank systems. It verifies ficium-auth RS256 tokens (portal users) and institution API keys (machine-to-machine), enforces RLS, and manages the full marketplace + pipeline lifecycle. For the full platform picture, see `ficium-portal/ARCHITECTURE.md`.

---

## 1. Core idea

The Portal's security model lives in the **database**: RLS policies and `SECURITY DEFINER` functions that resolve the caller through `auth.uid()`. This service replicates PostgREST's behaviour:

1. Verify the caller (RS256 JWT or SHA-256 API key lookup).
2. Open a DB transaction and `set_config('request.jwt.claims', <claims>, true)` + `SET LOCAL ROLE authenticated`.
3. Run the existing queries — RLS enforces tenant isolation unchanged.

No business logic is duplicated from the database; the API is a thin, portable shell around the SQL security layer.

---

## 2. Structure

```
app/
  api/        institutions, members, groups, approvals, marketplace, catalog,
              documents, benefits, pipeline, pipeline_templates, notifications,
              admin, auth_provision, public,
              api_keys, webhooks,          ← key management
              v1/marketplace               ← /v1/ public API
  core/       config.py, db.py, security.py (JWT),
              api_keys.py (key verification), webhooks.py (dispatcher)
  deps.py     current_claims / api_key_claims / api_or_jwt_claims / require_scope
  main.py     app + router wiring
db/
  000_auth_shim.sql, 001_workflow.sql, 003_expiry_notify.sql, 004_accept_bid_reveal.sql
docs/
  ADR-001 … ADR-002 … API-INTEGRATION-GUIDE.md
```

---

## 3. Deployment topology

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Institution LOS / Middleware / Scripts                                      │
│  Bearer: fic_live_<hex>  (API key)                                          │
└─────────────────────────┬───────────────────────────────────────────────────┘
                          │                Bearer: RS256 JWT
                          │        ┌───────────────────────────────────────┐
                          │        │   ficium-portal (Vercel SPA)          │
                          │        │   Browser — human operators           │
                          │        └───────────────┬───────────────────────┘
                          │                        │
                          ▼                        ▼
               ┌──────────────────────────────────────────────────┐
               │              ficium-portal-api                    │
               │              (FastAPI · Railway)                  │
               │                                                   │
               │  /api-keys   /webhooks   /v1/*                   │
               │  api_keys.py — SHA-256 key lookup                │
               │  webhooks.py — HMAC-signed delivery, retry       │
               │                                                   │
               │  /institutions  /members  /marketplace  etc.      │
               │  security.py — RS256 JWT + JWKS cache            │
               │  deps.py — api_or_jwt_claims / require_scope     │
               │  tenant_session() — SET jwt.claims + ROLE        │
               └──────────┬──────────────────────┬───────────────┘
                          │                      │
           psycopg2       │  :6543               │  APP_DATABASE_URL
           tx pooler      ▼                      ▼
          ┌───────────────────────────┐ ┌────────────────────────────┐
          │  Portal DB (Institution)  │ │  App DB (Consumer)         │
          │  egwobcajdlragubtkpqp    │ │  wixfhjlsjkiwfvqewvmt      │
          │  ap-southeast-1          │ │  ap-south-1                │
          │                          │ │                            │
          │  institution.*           │ │  public.clients            │
          │   └── api_key            │ │  public.requests           │
          │   └── webhook            │ │  public.kyc_submissions    │
          │   └── webhook_delivery   │ │  (Phase 2 PII fetch only)  │
          │  marketplace.*           │ └────────────────────────────┘
          │  governance.*            │
          │  catalog.*               │
          │  audit.*                 │        ┌────────────────────┐
          └───────────────────────────┘        │   ficium-auth      │
                                               │  (Railway)         │
                          JWKS (5 min cache)◄──┤  /.well-known/     │
                                               │  jwks.json         │
                                               └────────────────────┘

ficium (Vercel SPA — consumer app)
  │  X-Service-Secret
  ▼
/public/*  routes (no JWT)
```

---

## 4. Two authentication paths

### 4a. RS256 JWT (portal users)

`app/core/security.py` — `verify_token()`:
1. Decode token header → extract `kid`.
2. Fetch JWKS from ficium-auth (cached 5 min; force-refresh on unknown kid).
3. Verify RS256 signature, `iss=ficium-auth`, `aud=authenticated`, `exp`.
4. Return claims dict. `deps.current_claims()` wraps this as a FastAPI dependency.
5. `tenant_session(claims)` opens DB transaction, runs `SET request.jwt.claims` + `SET LOCAL ROLE authenticated`.

### 4b. Institution API keys (machine-to-machine)

`app/core/api_keys.py` — `verify_api_key()`:
1. Key format: `fic_live_<64 hex chars>` (32 random bytes).
2. SHA-256(raw_key) → lookup in `institution.api_key.key_hash`.
3. Validate: `active=true`, `mc_status='approved'`, `revoked_at IS NULL`, `expires_at > now()`.
4. Update `last_used_at` + `last_used_ip` (best-effort, non-blocking).
5. Return `{institution_id, scopes, key_id, auth_method="api_key"}`.

`deps.api_or_jwt_claims()` detects the path by prefix (`fic_live_` → key, anything else → JWT) and returns a unified claims dict. `deps.require_scope("scope")` is a dependency factory that gate-checks scope for API key callers (JWT callers bypass scope checks — they have full portal access).

Keys require maker-checker approval before activation. Raw key never stored — only SHA-256 hash. Shown to the user exactly once at creation.

---

## 5. Three DB session types

| Session | DSN variable | Role | RLS | When used |
|---------|-------------|------|-----|-----------|
| `tenant_session(claims)` | `DATABASE_URL` | `authenticated` | Enforced | All institution data routes (JWT path) |
| `service_session()` | `DATABASE_URL` | `postgres` | Bypassed | API key routes, webhooks, admin, s2s |
| `app_service_session()` | `APP_DATABASE_URL` | `postgres` | Bypassed | Phase 2 PII fetch, marketplace sync |

API key routes use `service_session()` with explicit `WHERE institution_id = :iid` filtering from claims — the equivalent of RLS but at the SQL layer, since API keys don't carry JWT claims for `auth.uid()`.

---

## 6. Webhook dispatcher

`app/core/webhooks.py` — `dispatch_event(institution_id, event_type, payload)`:

```
dispatch_event("f192050a-...", "bid.accepted", {...})
  → service_session()
  → SELECT institution.webhook WHERE institution_id=? AND event_types @> [event_type] AND active=true
  → asyncio.gather(*[_fire_single(wh, ...) for wh in webhooks])
      → HMAC-SHA256(signing_secret, payload_bytes) → X-Ficium-Signature-256
      → POST endpoint_url (timeout=webhook.timeout_ms)
      → on failure: sleep 5s/25s/125s, retry up to retry_max
      → INSERT institution.webhook_delivery (status, attempts, response_status, ...)
      → UPDATE institution.webhook (last_fired_at, failure_count)
      → if failure_count >= 10: SET active=false
```

Called with `asyncio.create_task()` so delivery is fire-and-forget from the API response. Current event emission points:
- `POST /v1/bids` → `bid.accepted` (confirmation to LOS after successful submission)
- `PUT /v1/pipeline/.../advance` → `pipeline.stage_changed`
- `POST /public/requests/:id/accept-bid` → `bid.accepted` (winning institution notified)

---

## 7. /v1/ versioned public API

All routes under `/v1/` form a stable, versioned contract for institution integrations. Breaking changes will go under `/v2/`. Key design properties:

- Accepts API key **or** JWT (unified via `api_or_jwt_claims`)
- Scope-gated at the dependency layer (`require_scope`)
- Server-enforced compliance check on bid submission (`product_config` lookup)
- Explicit `WHERE institution_id` filtering throughout (no RLS reliance for API key callers)
- Fires webhooks on state changes
- OpenAPI spec auto-generated at `/docs` and `/openapi.json`

---

## 8. Marketplace lifecycle

```
Sync (App DB → Portal DB):
  pg_net trigger on public.requests INSERT
  → POST /marketplace/sync-requests
  → marketplace.ingest_app_request()

Bid submission (API key path — LOS):
  POST /v1/bids
  → compliance gate (product_config check)
  → INSERT marketplace.bid
  → dispatch_event(bid.accepted)

Bid submission (JWT path — portal human):
  POST /marketplace/bids
  → governance.submit_for_approval()
  POST /approvals/{id}/approve (checker)
  → INSERT marketplace.bid
  → pg_net → bid-notify → Resend email

Bid acceptance (consumer):
  POST /public/requests/{id}/accept-bid  (s2s, Vercel)
  → marketplace.accept_bid() atomic:
      bid → accepted, others → rejected
      request → accepted, bid_acceptance PII row
      pipeline created from institution.pipeline_template
  → dispatch_event(bid.accepted) → institution webhook

Pipeline advance (LOS):
  PUT /v1/pipeline/{id}/stages/{sid}/advance
  → stage: in_progress → completed
  → next stage: pending → in_progress (or pipeline completed)
  → dispatch_event(pipeline.stage_changed)

Bid window close (cron):
  POST /marketplace/close-expired  (GitHub Actions, every 30 min)
  → marketplace.close_expired_windows()
```

---

## 9. Portability (non-Supabase deployments)

For client-cloud or on-prem Postgres, run `db/000_auth_shim.sql` before Portal migrations. It recreates `auth.uid()`, `auth.jwt()`, `auth.role()`, and the `authenticated`/`anon` roles so RLS policies work unchanged on any Postgres. Do **not** run on Supabase — the platform owns those objects there.

---

## 10. Connection constraints

- **Transaction pooler only (port 6543):** psycopg2 (sync). Supabase blocks direct port 5432 on pooled plans.
- **Username:** `postgres.<project-ref>` (pooler tenant prefix, not plain `postgres`).
- **`SET LOCAL ROLE authenticated`:** emitted inside `tenant_session()` — required because the pooler connects as `postgres` which carries `BYPASSRLS`. Without this, all RLS policies are silently inert. (This was a critical incident on 30 June 2026 — see `db.py` incident notes.)
- **Sync driver only:** asyncpg requires a direct persistent connection for session setup, which pgbouncer transaction mode doesn't support.
