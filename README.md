# ficium-portal-api

Portable data API for the Ficium Portal and the Ficium App marketplace. Verifies ficium-auth RS256 tokens, enforces RLS, handles the marketplace lifecycle, and serves as the server-to-server bridge between the App and Portal.

Built with FastAPI. Deployed on Railway (`ficium-portal-api-production.up.railway.app`).

---

## Why this service exists

The Portal's security model lives in the database: RLS policies and `SECURITY DEFINER` functions that resolve the caller through `auth.uid()`. On Supabase, PostgREST injects the verified JWT into `request.jwt.claims` so those work. This service does the **same thing without PostgREST** — it verifies the ficium-auth token itself, sets `request.jwt.claims`, and runs the existing queries unchanged.

That portability is the point: the same API runs against Supabase (SaaS), a client's managed Postgres, or on-prem Postgres, with no change to the database security layer. See `docs/ADR-001-portable-portal-data-layer.md`.

---

## Endpoints

### Institution (Bearer: ficium-auth RS256 JWT)

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
| `GET`  | `/webhooks` | Institution webhooks |
| `GET`  | `/audit` | Audit events |

### Public / Service-to-service (X-Service-Secret header)

| Method | Path | Caller | Purpose |
|--------|------|--------|---------|
| `GET`  | `/public/requests/{id}/bids` | Ficium App | Consumer bid list (double-blind) |
| `POST` | `/public/requests/bids/bulk` | Ficium App | Consumer bid list (bulk) |
| `POST` | `/public/requests/{id}/accept-bid` | Vercel `accept-bid.ts` | Phase 2 PII reveal + atomic acceptance |
| `POST` | `/marketplace/sync-requests` | pg_net (App DB) | Ingest new consumer requests |
| `POST` | `/marketplace/close-expired` | GitHub Actions (every 30 min) | Close expired bid windows |

---

## Auth

1. Every portal request carries `Authorization: Bearer <ficium-auth RS256 JWT>`.
2. `app/core/security.py` fetches the JWKS from ficium-auth (cached 5 min), matches the token's `kid`, verifies RS256 signature + `iss` / `aud` / `exp`.
3. `app/deps.py` extracts verified claims.
4. `tenant_session()` opens a DB transaction and runs:
   ```sql
   SELECT set_config('request.jwt.claims', '<claims json>', true);
   ```
   so `auth.uid()` resolves and RLS enforces tenant isolation — exactly as PostgREST would.

Service-to-service routes authenticate via `X-Service-Secret` header (constant-time `hmac.compare_digest`).

---

## Marketplace sync (App DB → Portal DB)

When a consumer submits a request on the Ficium App:
1. `trg_marketplace_sync` fires on `public.requests` INSERT/UPDATE (App DB)
2. `marketplace_sync.dispatch()` calls `net.http_post` → `POST /marketplace/sync-requests`
3. `sync_requests()` pulls open requests from App DB, enriches with consumer financial data, calls `marketplace.ingest_app_request()` on Portal DB

The sync endpoint connects to the App DB via `app_service_session()` (`APP_DATABASE_URL`). A pg_cron safety-net sweep runs every 5 minutes.

---

## Bid notification flow

On `marketplace.bid` INSERT (Portal DB):
1. `trg_bid_notify` fires → `bid_notify.dispatch(bid_id)`
2. pg_net calls `POST ficium.vercel.app/api/internal { action: 'bid-notify', ...bid data }`
3. Vercel handler resolves consumer from App DB, writes `public.notifications`, sends Resend email

---

## Local development

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt

cp .env.example .env   # fill in DATABASE_URL, APP_DATABASE_URL, etc.

uvicorn app.main:app --reload --port 8000
```

### Tests

```bash
pytest                    # unit tests
pytest tests/test_integration.py   # integration (needs DB)
```

CI runs ruff + mypy + pytest on every push.

---

## Environment variables

| Variable | Notes |
|---|---|
| `DATABASE_URL` | Institution Supabase transaction pooler (`postgres.<ref>:6543`) |
| `APP_DATABASE_URL` | App Supabase connection (for sync + Phase 2 PII fetch) |
| `APP_SERVICE_SECRET` | Shared secret for s2s auth (X-Service-Secret) |
| `AUTH_JWKS_URL` | `https://ficium-auth-production.up.railway.app/.well-known/jwks.json` |
| `AUTH_ISSUER` | `ficium-auth` |
| `AUTH_AUDIENCE` | `authenticated` (matches ficium-auth's `aud` claim) |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `DEPLOYMENT_MODEL` | `saas` / `client_cloud` / `on_prem` |

**Connection rules:**
- Use the **transaction pooler (port 6543)**, not direct (5432)
- Username: `postgres.<project-ref>` (pooler tenant prefix)
- Use **psycopg2** (sync), not asyncpg — asyncpg needs a direct connection
- Do NOT `SET LOCAL ROLE` — pgbouncer transaction mode prohibits it

---

## DB files

| File | Purpose | Target DB |
|------|---------|-----------|
| `db/000_auth_shim.sql` | Recreates `auth.uid()/jwt()/role()` for non-Supabase Postgres | Portal DB (non-Supabase only) |
| `db/001_workflow.sql` | Workflow/maker-checker helpers | Portal DB |
| `db/003_expiry_notify.sql` | `close_expired_windows()` — fires `request-expired` via pg_net | Portal DB |
| `db/004_accept_bid_reveal.sql` | `accept_bid()` — includes bid financials in return payload | Portal DB |

---

## Deployment (Railway)

1. Deploy from GitHub — Railway uses the `Dockerfile`.
2. Set all environment variables above.
3. Under Settings → Networking, generate a public domain, port 8000.
4. `GET /health` → `{"status":"ok",...}` confirms liveness.
5. `GET /members/my-group` without a token → `401` confirms auth gate is live.

GitHub Actions workflows:
- `ci.yml` — ruff + mypy + pytest on every push
- `close-bid-windows.yml` — `POST /marketplace/close-expired` every 30 min
- `keepalive.yml` — `GET /health` every 5 min to prevent Railway cold start

---

## Project layout

```
app/
  api/        institutions.py, members.py, groups.py, approvals.py,
              marketplace.py, catalog.py, documents.py, benefits.py,
              admin.py, auth_provision.py, public.py
  core/       config.py, db.py (tenant_session + app_service_session),
              security.py (JWKS/RS256 verify)
  deps.py     claim extraction → request context
  main.py     app + router wiring
db/
  000_auth_shim.sql
  001_workflow.sql
  003_expiry_notify.sql
  004_accept_bid_reveal.sql
  README.md
docs/
  ADR-001-portable-portal-data-layer.md
  ADR-002-identity-migration.md
tests/
  test_health.py
  test_integration.py
```
