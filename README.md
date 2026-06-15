# ficium-portal-api

Portable data API for the Ficium Portal. Verifies ficium‑auth RS256 tokens, then serves institution‑scoped data with Postgres row‑level security enforced. Exposes the maker‑checker approval RPCs.

Built with FastAPI. Deployed on Railway. Connects to Supabase Postgres via the transaction pooler.

---

## Why this service exists

The Portal's security model lives in the database: RLS policies and SECURITY DEFINER functions that resolve the caller through `auth.uid()`. On Supabase, PostgREST injects the verified JWT into `request.jwt.claims` so those work. This service does the **same thing without PostgREST** — it verifies the ficium‑auth token itself, sets `request.jwt.claims`, and runs the existing queries unchanged.

That portability is the point: the same API runs against Supabase (SaaS), a client's managed Postgres, or on‑prem Postgres, with no change to the database security layer. See `docs/ADR-001-portable-portal-data-layer.md`.

---

## Endpoints

| Method | Path | Replaces (old Supabase call) |
|--------|------|------------------------------|
| `GET`  | `/health` | — |
| `GET`  | `/institutions/me` | `detect_portal_user_type` + institution queries |
| `GET`  | `/members/me` | `institution_members` self‑query |
| `GET`  | `/members/my-group` | `get_my_group()` RPC |
| `GET`  | `/members` | `institution_members` list |
| `GET`  | `/approvals/pending` | `pending_actions` query |
| `POST` | `/approvals/submit` | `submit_for_approval()` RPC |
| `POST` | `/approvals/{id}/approve` | `approve_action()` RPC |
| `POST` | `/approvals/{id}/reject` | `reject_action()` RPC |
| `GET`  | `/webhooks` | `institution_webhooks` query |
| `GET`  | `/audit` | `audit_events` query |

> Marketplace requests, bids, and products are **not** served here — those read cross‑project data from the Ficium App's Supabase project and are queried by the frontend directly.

---

## How auth works

1. Every request carries `Authorization: Bearer <ficium-auth RS256 JWT>`.
2. `app/core/security.py` fetches the JWKS from ficium‑auth (cached), matches the token's `kid`, and verifies the RS256 signature plus `iss` / `aud` / `exp`.
3. `app/deps.py` extracts the verified claims.
4. For data routes, `tenant_session()` opens a DB transaction and runs:
   ```sql
   SELECT set_config('request.jwt.claims', '<claims json>', true);
   ```
   so `auth.uid()` resolves and RLS enforces tenant isolation — exactly as PostgREST would.

Platform admins (`user_role` of `admin` / `super_admin`) are gated by role before any DB session opens, so they never depend on an institution row.

---

## Local development

### Prerequisites

- Python 3.12
- Access to the Supabase project (institution schema applied)

### Setup

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt   # for tests

cp .env.example .env   # then fill in DATABASE_URL etc.

uvicorn app.main:app --reload --port 8000
```

### Tests

```bash
pytest
```

CI runs lint, type‑check, and tests on every push (`.github/workflows/ci.yml`).

---

## Configuration (environment variables)

| Variable | Notes |
|----------|-------|
| `DATABASE_URL` | **Supabase transaction pooler** DSN. `postgresql://postgres.<ref>:<pw>@aws-1-ap-southeast-1.pooler.supabase.com:6543/postgres`. URL‑encode special chars in the password (`@` → `%40`). |
| `AUTH_JWKS_URL` | `https://ficium-auth-production.up.railway.app/.well-known/jwks.json` |
| `AUTH_ISSUER` | `ficium-auth` |
| `AUTH_AUDIENCE` | `ficium-portal` (must match the token's `aud`) |
| `ALLOWED_ORIGINS` | Comma‑separated exact origins |
| `ALLOWED_ORIGIN_REGEX` | Regex for Vercel preview URLs, e.g. `^https://ficium-portal[a-z0-9\-]*\.vercel\.app$` |
| `DEPLOYMENT_MODEL` | `saas` / `client_cloud` / `on_prem` |

### Connection gotchas

- Use the **transaction pooler (6543)**, not the direct connection (5432) — Supabase blocks direct external connections on the free plan.
- Username must be `postgres.<project-ref>` (the pooler tenant), not plain `postgres`.
- The pooler host is region‑specific (`aws-1-…`, not `aws-0-…`). Copy the exact string from the Supabase dashboard.
- This service uses **psycopg2** (sync), not asyncpg, because asyncpg needs a direct connection.
- It does **not** issue `SET LOCAL ROLE` — pgbouncer transaction mode and the pooler user make that both unnecessary and impossible.

---

## Deployment (Railway)

1. Deploy from GitHub; Railway builds the `Dockerfile`.
2. Set the environment variables above.
3. Under **Settings → Networking**, generate a public domain and set the target port to **8000**.
4. Confirm `GET /health` → `{"status":"ok",...}`.
5. Confirm `GET /members/my-group` without a token returns `401 Missing bearer token` (proves the auth gate is live).

---

## Portable deployments (client cloud / on-prem)

For non‑Supabase Postgres, run `db/000_auth_shim.sql` **before** the institution migrations. It recreates the `auth.uid()` / `auth.jwt()` helpers and the `authenticated` role that Supabase provides natively, so the existing RLS policies load and run unchanged. See `db/README.md`. Do **not** run the shim on Supabase — the platform owns those objects there.

---

## Project layout

```
app/
  api/        institutions.py, members.py, approvals.py,
              marketplace.py, catalog.py
  core/       config.py, db.py, security.py
  deps.py                                   — request dependencies (auth + session)
  main.py                                   — app + router wiring
db/
  000_auth_shim.sql                         — portable-Postgres auth shim
  README.md
docs/
  ADR-001-portable-portal-data-layer.md
tests/        test_health.py
```
