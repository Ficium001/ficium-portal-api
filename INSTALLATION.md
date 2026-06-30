# ficium-portal-api — Installation

_Last updated: 27 June 2026_

---

## Prerequisites

- Python 3.12
- Access to the Institution Supabase project (`egwobcajdlragubtkpqp`) with Portal schema applied
- Access to the App Supabase project (`wixfhjlsjkiwfvqewvmt`) for sync + Phase 2 PII
- ficium-auth running (for JWKS verification)

---

## Local setup

```bash
# 1. Clone and install
git clone https://github.com/Ficium001/ficium-portal-api.git
cd ficium-portal-api
pip install -r requirements.txt
pip install -r requirements-dev.txt   # includes ruff, mypy, pytest

# 2. Configure
cp .env.example .env
```

Fill in `.env`:

```
DATABASE_URL=postgresql://postgres.<institution-ref>:<pw>@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres
APP_DATABASE_URL=postgresql://postgres.<app-ref>:<pw>@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
APP_SERVICE_SECRET=<shared secret — same value set in Vercel + Supabase Vault>
AUTH_JWKS_URL=https://ficium-auth-production.up.railway.app/.well-known/jwks.json
AUTH_ISSUER=ficium-auth
AUTH_AUDIENCE=authenticated
ALLOWED_ORIGINS=https://ficium-portal.vercel.app,https://portal.ficium.net
# Regex covering all Vercel preview deployments automatically.
# IMPORTANT: if you set this in Railway, do not set it to an empty
# string — that disables regex matching entirely. Leave it UNSET to
# use the safe code default, or copy the value below explicitly.
ALLOWED_ORIGIN_REGEX=^https://ficium-portal[a-z0-9.\-]*\.vercel\.app$
DEPLOYMENT_MODEL=saas
```

```bash
# 3. Run
uvicorn app.main:app --reload --port 8000

# 4. Verify
curl http://localhost:8000/health
# → {"status":"ok","env":"development","model":"saas"}
```

---

## DB migrations (non-Supabase only)

On Supabase SaaS, skip `000_auth_shim.sql` — Supabase provides `auth.*` natively.

For on-prem or client-cloud Postgres:

```sql
-- 1. Auth shim (must be first)
\i db/000_auth_shim.sql

-- 2. Portal migrations (from ficium-portal repo, in order)
-- Apply all supabase/migrations/*.sql files

-- 3. Workflow helpers
\i db/001_workflow.sql

-- 4. Expiry notification dispatch
\i db/003_expiry_notify.sql

-- 5. accept_bid() with financials
\i db/004_accept_bid_reveal.sql
```

---

## Running tests

```bash
pytest                           # all tests
pytest tests/test_health.py      # health check only
pytest tests/test_integration.py # needs running DB (set DATABASE_URL)
```

CI: ruff + mypy + pytest run on every push to any branch via `.github/workflows/ci.yml`.

---

## Connection troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `could not translate host name` | Wrong pooler host | Copy exact string from Supabase dashboard (region-specific) |
| `password authentication failed` | Wrong username | Must be `postgres.<project-ref>`, not `postgres` |
| `SSL connection required` | Missing SSL param | Add `?sslmode=require` to DSN |
| `too many connections` | Using direct port | Use pooler port 6543, not 5432 |
| JWKS fetch failing | ficium-auth not running | Check Railway logs; confirm `/health` on ficium-auth |

---

## Railway deployment

1. Connect GitHub repo `Ficium001/ficium-portal-api` to Railway
2. Railway auto-detects `Dockerfile` and builds
3. Set all environment variables in Railway project settings
4. Set target port to **8000** under Settings → Networking
5. Generate a public domain
6. Confirm: `GET /health` → `200`, `GET /members/my-group` without token → `401`

**Auto-deploy:** Railway deploys on every push to `main`.

**Vault secrets:** The App DB vault secrets (`portal_api_url`, `app_service_secret`) must be set on the App Supabase project for pg_net triggers to reach this service:

```sql
-- Run on App DB (wixfhjlsjkiwfvqewvmt)
SELECT vault.create_secret('https://ficium-portal-api-production.up.railway.app', 'portal_api_url');
SELECT vault.create_secret('<APP_SERVICE_SECRET>', 'app_service_secret');
```
