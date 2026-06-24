# ficium-portal-api — Architecture

_Last updated: 24 June 2026_

The **portable data API** for the Ficium Portal. It verifies ficium-auth RS256
tokens and serves institution-scoped data with Postgres row-level security
enforced — doing exactly what Supabase's PostgREST does, but without PostgREST,
so the same database security layer runs on any Postgres. This is the keystone
of the platform's lift-and-shift story (ADR-001).

---

## 1. The core idea

The Portal's security model lives in the **database**: RLS policies and
`SECURITY DEFINER` functions that resolve the caller through `auth.uid()`. On
Supabase, PostgREST injects the verified JWT into `request.jwt.claims` so those
work. This service does the same thing itself:

1. Verify the ficium-auth token (RS256, against cached JWKS).
2. Open a DB transaction and `set_config('request.jwt.claims', <claims>, true)`.
3. Run the existing queries — RLS enforces tenant isolation unchanged.

No business logic is duplicated from the database; the API is a thin, portable
shell around the SQL security layer.

---

## 2. Structure

```
app/
  api/      institutions, members, groups, approvals, marketplace, catalog,
            documents, benefits, admin, auth_provision, public
  core/     config.py, db.py (tenant_session / pooler), security.py (JWKS/RS256)
  deps.py   claim extraction → request context
db/
  000_auth_shim.sql   auth.uid()/jwt()/role() + authenticated/anon (non-Supabase)
  001_workflow.sql    workflow helpers
  README.md           load order
docs/
  ADR-001-portable-portal-data-layer.md
  ADR-002-identity-migration.md
```

Admin routes are gated by role (`admin` / `super_admin`) **before** any DB
session opens, so platform staff never depend on an institution row.

---

## 3. Deployment topology

```
   ficium-portal (Vercel SPA)
        │  Authorization: Bearer <ficium-auth RS256 JWT>
        ▼
┌─────────────────────────────────────────────┐     JWKS (cached)
│            ficium-portal-api                 │◄──────────────────┐
│            (FastAPI · Railway)               │                   │
│                                              │            ┌──────┴───────┐
│  security.py  verify RS256 (kid→JWKS)        │            │ ficium-auth  │
│  deps.py      extract claims                 │            │ /.well-known │
│  tenant_session()  SET request.jwt.claims    │            └──────────────┘
└───────────────────────┬─────────────────────┘
                        │ psycopg2, transaction pooler :6543
                        ▼
            ┌─────────────────────────────┐
            │  Postgres (RLS enforced)    │
            │  SaaS:  Supabase pooler     │
            │  Cloud: client managed PG   │
            │  On-prem: PG + auth_shim    │
            └─────────────────────────────┘
```

**Connection facts that bite:** Supabase direct port 5432 is blocked on free
plan → connect via the **transaction pooler (6543)** with `psycopg2` (not
asyncpg). The pooler username is `postgres.<project-ref>`, host is
region-specific. RLS keys on `auth.uid()` (the GUC), not `current_role`, so the
service does not (and cannot, through the pooler) `SET ROLE`.

---

## 4. Portability (ADR-001)

The same image runs against three substrates with no change to the security
layer:

| Model | Postgres | Auth helpers |
|-------|----------|--------------|
| SaaS | Supabase pooler | provided by Supabase (skip shim) |
| Client cloud | client-managed PG | `db/000_auth_shim.sql` first |
| On-prem | PG in the bank network | `db/000_auth_shim.sql` first |

The shim recreates `auth.uid()/auth.jwt()/auth.role()` and the
`authenticated`/`anon` roles, then the Portal migrations load **unchanged**. See
`db/README.md` for load order.
