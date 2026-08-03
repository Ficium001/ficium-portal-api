# ficium-portal-api

Portable data API for the Ficium Portal and the Ficium App marketplace. Verifies ficium-auth RS256 tokens **and institution API keys**, enforces RLS, handles the full marketplace lifecycle (bids, pipeline, webhooks), and serves as the server-to-server bridge between the App and Portal.

Built with FastAPI. Deployed on Railway (`ficium-portal-api-production.up.railway.app`).

---

## Why this service exists

The Portal's security model lives in the database: RLS policies and `SECURITY DEFINER` functions that resolve the caller through `auth.uid()`. On Supabase, PostgREST injects the verified JWT into `request.jwt.claims` so those work. This service does the **same thing without PostgREST** — it verifies the ficium-auth token itself, sets `request.jwt.claims`, and runs the existing queries unchanged.

That portability is the point: the same API runs against Supabase (SaaS), a client's managed Postgres, or on-prem Postgres, with no change to the database security layer. See `docs/ADR-001-portable-portal-data-layer.md`.

As of July 2026 the API is also the **institution integration layer** — bank LOS systems, middleware, and internal scripts connect via scoped API keys rather than the browser JWT. This is the foundation for MCB and future bank integrations. See `docs/API-INTEGRATION-GUIDE.md`.

---

## Authentication

Two auth paths are accepted. Every request carries `Authorization: Bearer <token>`:

| Auth type | Token format | Who uses it | Resolved by |
|-----------|-------------|-------------|-------------|
| RS256 JWT | `eyJ...` | Portal browser users | ficium-auth JWKS (5 min cache) |
| Institution API key | `fic_live_<64 hex chars>` | Bank LOS, middleware, scripts | `institution.api_key` (SHA-256 lookup) |

**Portal users** (JWT): see `app/core/security.py` — JWKS fetched from ficium-auth, `kid` matched, RS256 + `iss`/`aud`/`exp` verified. Claims injected into DB session via `tenant_session()`.

**API keys** (machine): see `app/core/api_keys.py` — key hashed with SHA-256, looked up in `institution.api_key`. Checked for `active=true`, `mc_status='approved'`, not expired, not revoked. Scopes enforced per endpoint. `last_used_at` updated on each call.

**Service-to-service** (`/public/*`): `X-Service-Secret` header (constant-time `hmac.compare_digest`). Used by the Ficium App Vercel backend only.

---

## Endpoints

### Institution — Portal users (Bearer: RS256 JWT)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | Liveness |
| `GET`  | `/institutions/me` | Institution profile + onboarding state |
| `GET`  | `/members/me` | Current member profile |
| `GET`  | `/members/my-group` | My group + module access |
| `GET`  | `/members` | All institution members |
| `GET`  | `/approvals/pending` | Pending maker-checker actions |
| `POST` | `/approvals/submit` | Submit action for approval |
| `POST` | `/approvals/{id}/approve` | Approve (checker) |
| `POST` | `/approvals/{id}/reject` | Reject (checker) |
| `GET`  | `/marketplace/requests` | Open requests (institution view) |
| `GET`  | `/marketplace/my-bids` | Institution's own bids |
| `POST` | `/marketplace/bids` | Submit bid (via maker-checker) |
| `GET`  | `/marketplace/bids/{bid_id}` | Single bid detail |
| `GET`  | `/catalog/products` | Product catalogue |
| `GET`  | `/groups` | Institution groups |
| `GET`  | `/pipeline-templates` | Pipeline template management |
| `GET`  | `/pipeline/{loan_id}` | Active loan pipeline |
| `GET`  | `/notifications` | Institution notifications |
| `GET`  | `/audit` | Audit events |

### API Key Management — Portal users (Bearer: RS256 JWT)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api-keys` | List institution API keys |
| `POST` | `/api-keys` | Request new key (maker-checker) |
| `PUT`  | `/api-keys/{id}/approve` | Approve pending key (checker) |
| `POST` | `/api-keys/{id}/revoke` | Revoke active key |
| `DELETE` | `/api-keys/{id}` | Delete pending/revoked key |

### Webhook Management — Portal users (Bearer: RS256 JWT)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/webhooks` | List registered webhooks |
| `POST` | `/webhooks` | Register new webhook endpoint |
| `PUT`  | `/webhooks/{id}` | Update webhook config |
| `DELETE` | `/webhooks/{id}` | Delete webhook |
| `POST` | `/webhooks/{id}/test` | Fire test ping event |
| `GET`  | `/webhooks/{id}/deliveries` | Delivery log (paginated) |
| `POST` | `/webhooks/{id}/reset-failures` | Clear failure count, re-enable |

### /v1/ Public API — Institution API keys OR portal JWT

All endpoints accept `fic_live_*` API keys with the appropriate scope, or a portal RS256 JWT.

| Method | Path | Scope required | Purpose |
|--------|------|---------------|---------|
| `GET`  | `/v1/requests` | `marketplace:read` | Browse open marketplace requests |
| `GET`  | `/v1/bids` | `bids:read` | List institution's own bids |
| `POST` | `/v1/bids` | `bids:write` | Submit bid from LOS |
| `PUT`  | `/v1/pipeline/{loan_id}/stages/{stage_id}/advance` | `pipeline:write` | Advance pipeline stage from LOS |
| `GET`  | `/v1/analytics/summary` | `analytics:read` | Performance metrics |

### Public / Service-to-service (X-Service-Secret header)

| Method | Path | Caller | Purpose |
|--------|------|--------|---------|
| `GET`  | `/public/requests/{id}/bids` | Ficium App | Consumer bid list (double-blind) |
| `POST` | `/public/requests/bids/bulk` | Ficium App | Consumer bid list (bulk) |
| `POST` | `/public/requests/{id}/accept-bid` | Vercel `accept-bid.ts` | Phase 2 PII reveal + atomic acceptance |
| `POST` | `/marketplace/sync-requests` | pg_net (App DB) | Ingest new consumer requests |
| `POST` | `/marketplace/close-expired` | GitHub Actions (every 30 min) | Close expired bid windows |
| `GET`  | `/public/requests/{id}/pipeline` | Ficium App | Borrower-visible pipeline stages |

---

## Security

Full model in **[SECURITY.md](./SECURITY.md)**. In brief:

- **Tenant isolation** via RLS — enforced by `tenant_session()` which sets JWT
  claims *and* `SET LOCAL ROLE authenticated` (the pooler is `postgres`/BYPASSRLS,
  so the role switch is what actually engages RLS). All tenant tables are
  `ENABLE` + `FORCE ROW LEVEL SECURITY`.
- **Double-blind marketplace** — `marketplace.request` holds only an opaque
  `consumer_id`; PII is revealed only on bid acceptance.
- **Rate limiting** (`core/ratelimit.py`), **SSRF guard** on webhook URLs
  (`core/ssrf.py`), **admin guard** at router level, **RS256-pinned** JWT
  verification, and **allowlist-only** dynamic SQL.
- Adding a tenant-scoped table? See the checklist in SECURITY.md §2.

---

## API Key scopes

| Scope | What it permits |
|-------|----------------|
| `marketplace:read` | Browse open requests |
| `bids:read` | View own bids |
| `bids:write` | Submit bids |
| `pipeline:read` | View pipeline status |
| `pipeline:write` | Advance pipeline stages |
| `analytics:read` | Pull performance metrics |
| `documents:write` | Upload pipeline documents |
| `webhooks:manage` | Register/update webhooks |

Keys require maker-checker approval (`mc_status = 'approved'`) before they are active. Raw key value is shown once at creation — store it securely.

---

## Webhook events

When a key business event occurs, Ficium fires a signed HTTP POST to all institution webhooks subscribed to that event type.

| Event type | Fired when |
|-----------|-----------|
| `request.new` | New borrower request posted (eligibility match) |
| `bid.accepted` | Borrower accepted the institution's bid |
| `bid.rejected` | Bid expired or request closed without acceptance |
| `pipeline.stage_changed` | Pipeline stage advanced or completed |
| `identity.revealed` | Phase 2 borrower identity made available |

**Signature verification** — every delivery includes:
```
X-Ficium-Event: bid.accepted
X-Ficium-Delivery: <uuid>
X-Ficium-Timestamp: <unix epoch>
X-Ficium-Signature-256: sha256=<hmac-sha256 hex>
```

Verify: `HMAC-SHA256(signing_secret, request_body) == signature`. Signing secret is shown once at webhook registration.

**Retry policy** — exponential backoff: 5s, 25s, 125s (3 attempts max by default). Webhook auto-disabled after 10 consecutive failures. Reset via `POST /webhooks/{id}/reset-failures`.

---

## Marketplace lifecycle

```
POST /marketplace/sync-requests
  ← called by pg_net (App DB trigger) or pg_cron (5min sweep)
  → pull open requests from App DB
  → enrich with consumer financial snapshot
  → marketplace.ingest_app_request() on Portal DB

POST /v1/bids  (API key path — institution LOS)
  → validate request open + bid window active
  → enforce product_config compliance gate
  → INSERT marketplace.bid, status=submitted
  → fire webhook: bid.accepted (confirmation to LOS)

POST /marketplace/bids  (portal JWT path — human operator)
  → governance.submit_for_approval(action='bid.submit')
  → returns pending governance.action id

POST /approvals/{id}/approve  (checker)
  → governance.approve_action()
  → _execute_action('bid.submit') → INSERT marketplace.bid
  → trg_bid_notify fires (pg_net → bid-notify handler)

POST /public/requests/{id}/accept-bid  (s2s from Vercel)
  → ownership check (anon UUID)
  → fetch Phase 2 PII from App DB
  → marketplace.accept_bid() atomic:
      bid → accepted, others → rejected
      request → accepted + winning_bid_id
      bid_acceptance (PII stored on Portal DB)
      pipeline auto-created from institution template
  → fire webhook: bid.accepted (to winning institution)
  → returns institution contact + bid financials

PUT /v1/pipeline/{loan_id}/stages/{stage_id}/advance  (API key — LOS)
  → verify pipeline + stage ownership
  → stage: in_progress → completed
  → activate next stage, or complete pipeline if last
  → fire webhook: pipeline.stage_changed

POST /marketplace/close-expired  (GitHub Actions, every 30 min)
  → marketplace.close_expired_windows()
  → fire webhook: bid.rejected (institutions with open bids on expired requests)
```

---

## Local development

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt

cp .env.example .env   # fill in DATABASE_URL, APP_DATABASE_URL, etc.

uvicorn app.main:app --reload --port 8000
```

OpenAPI docs available at `http://localhost:8000/docs` once running.

### Tests

```bash
pytest                             # unit tests
pytest tests/test_integration.py  # integration (needs live DB)
```

CI runs ruff + mypy + pytest on every push.

---

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | Institution Supabase transaction pooler (`postgres.<ref>:6543`) |
| `APP_DATABASE_URL` | Yes | App Supabase connection (sync + Phase 2 PII fetch) |
| `APP_SERVICE_SECRET` | Yes | Shared secret for `X-Service-Secret` s2s auth |
| `AUTH_JWKS_URL` | Yes | `https://ficium-auth-production.up.railway.app/.well-known/jwks.json` |
| `AUTH_ISSUER` | Yes | `ficium-auth` |
| `AUTH_AUDIENCE` | Yes | `authenticated` |
| `ALLOWED_ORIGINS` | Yes | Comma-separated CORS origins |
| `ALLOWED_ORIGIN_REGEX` | No | Regex for preview deployments |
| `DEPLOYMENT_MODEL` | No | `saas` / `client_cloud` / `on_prem` (default: `saas`) |

**Connection rules:**
- Use the **transaction pooler (port 6543)**, not direct (5432)
- Username: `postgres.<project-ref>` (pooler tenant prefix)
- Use **psycopg2** (sync), not asyncpg — asyncpg needs a direct connection
- `SET LOCAL ROLE authenticated` is emitted inside `tenant_session()` to engage RLS — this is required and correct for pgbouncer transaction mode

---

## Project layout

```
app/
  api/
    api_keys.py         API key CRUD (list, create, approve, revoke, delete)
    webhooks.py         Webhook CRUD + delivery log + test ping
    v1/
      marketplace.py    /v1/ public API (requests, bids, pipeline, analytics)
    institutions.py     Institution profile
    members.py          Member + group queries
    groups.py           Group management
    approvals.py        Maker-checker workflow
    marketplace.py      Portal marketplace (human operators)
    catalog.py          Product catalogue
    documents.py        Compliance documents
    benefits.py         Bid benefits
    pipeline.py         Loan pipeline (portal view)
    pipeline_templates.py  Pipeline template settings
    notifications.py    In-portal notifications
    admin.py            Ficium admin endpoints
    auth_provision.py   User provisioning
    public.py           Server-to-server (X-Service-Secret)
  core/
    config.py           Pydantic settings (env vars)
    db.py               tenant_session / service_session / app_service_session
    security.py         RS256 JWT verification (JWKS cache)
    api_keys.py         API key SHA-256 verification + key generation
    webhooks.py         Webhook dispatcher (HMAC signing, retry, delivery log)
  deps.py               current_claims / api_key_claims / api_or_jwt_claims / require_scope
  main.py               App + router wiring
db/
  000_auth_shim.sql     auth.uid()/jwt()/role() (non-Supabase only)
  001_workflow.sql      Workflow helpers
  003_expiry_notify.sql close_expired_windows() + pg_net
  004_accept_bid_reveal.sql  accept_bid() with bid financials
docs/
  ADR-001-portable-portal-data-layer.md
  ADR-002-identity-migration.md
  API-INTEGRATION-GUIDE.md   ← Bank / LOS integration guide
tests/
  test_health.py
  test_api.py           108 HTTP-layer tests
  test_integration.py   DB-level integration tests
  INTEGRATION_SETUP.md
```

---

## Documentation

| Doc | Scope |
|---|---|
| `ARCHITECTURE.md` | This service's architecture — thin shell over the Portal DB's RLS |
| `DESIGN.md` | Why this service exists (ADR-001), endpoint design philosophy |
| `SECURITY.md` | Auth paths, tenant isolation mechanics, incident notes |
| `DATABASE.md` | This service owns no schema — points at `ficium-portal/supabase/migrations/` |
| `docs/platform/` | **Cross-repo platform docs** — full data dictionary (both DBs), full API reference, functional spec, security model. Kept identical across all three repos. |

---

## Deployment (Railway)

1. Deploy from GitHub — Railway uses the `Dockerfile`.
2. Set all environment variables above in Railway → Variables.
3. Under Settings → Networking, generate a public domain, port 8000.
4. `GET /health` → `{"status":"ok",...}` confirms liveness.
5. `GET /members/my-group` without a token → `401` confirms auth gate.

**GitHub Actions workflows:**
- `ci.yml` — ruff + mypy + pytest on every push
- `close-bid-windows.yml` — `POST /marketplace/close-expired` every 30 min
- `keepalive.yml` — `GET /health` every 5 min (prevent Railway cold start)
