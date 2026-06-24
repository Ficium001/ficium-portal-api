# ficium-portal-api — Installation

_Last updated: 24 June 2026_

## Prerequisites
- Python 3.12
- A Postgres reachable via connection string (Supabase pooler for SaaS)
- The ficium-auth JWKS URL (for token verification)

## Local setup
```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt      # tests/lint
cp .env.example .env
# set: DATABASE_URL (pooler), APP_DATABASE_URL (cross-project, if used),
#      AUTH_JWKS_URL, AUTH_ISSUER, AUTH_AUDIENCE
make run          # or: uvicorn app.main:app --reload
make test         # pytest (conftest.py provides fixtures)
```

## Database
- **SaaS (Supabase):** connect to the transaction pooler (port 6543), username
  `postgres.<project-ref>`. Schema is applied from the `ficium-portal` repo.
- **Client cloud / on-prem:** apply `db/000_auth_shim.sql` first, then the
  Portal migrations in order (see `db/README.md`).

## Deploy (Railway)
- Builder: Dockerfile. Healthcheck: `GET /health`.
- Set `DATABASE_URL` to the pooler connection; set the `AUTH_*` token-verification
  vars to point at ficium-auth.

## Configuration reference
| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Portal Postgres (Supabase pooler in SaaS) |
| `APP_DATABASE_URL` | Cross-project engine for App-owned reads, where applicable |
| `AUTH_JWKS_URL` | ficium-auth JWKS endpoint |
| `AUTH_ISSUER` / `AUTH_AUDIENCE` | Expected `iss` / `aud` on tokens |
