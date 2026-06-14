# ficium-portal-api

Portable data API for Ficium Portal. Replaces Supabase PostgREST: verifies
ficium-auth RS256 tokens (via JWKS) and runs every request through Postgres
with `request.jwt.claims` set, so the Portal's existing RLS policies and
maker-checker functions enforce tenant isolation unchanged — on SaaS,
client cloud, or on-premises.

See `ADR-001` for the full design. Architecture in one line:

    browser → ficium-portal-api (verify JWT, SET LOCAL ROLE authenticated,
              inject request.jwt.claims) → Postgres (RLS enforced)

## Run locally
    cp .env.example .env   # fill DATABASE_URL etc.
    pip install -r requirements-dev.txt
    uvicorn app.main:app --reload

## Layout
    app/core/db.py        per-request RLS session (the PostgREST replacement)
    app/core/security.py  ficium-auth JWKS verification
    app/deps.py           bearer → verified claims → scoped connection
    app/api/              resource routers (institutions, … expanding per ADR)
    db/000_auth_shim.sql  auth.* shim for non-Supabase Postgres
