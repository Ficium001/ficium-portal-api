# ficium-portal-api — Database

_Last updated: 24 June 2026_

This service **does not own a schema**. It is a portable access layer over the
Portal's database, whose schema is defined in
`ficium-portal/supabase/migrations/` and documented in that repo's `DATABASE.md`
(the live v2 seven-schema design: `identity`, `catalog`, `institution`,
`marketplace`, `governance`, `admin`, `audit`).

What lives **here** is the portability and workflow SQL that makes that schema
runnable off Supabase.

---

## Files in `db/`

| File | Purpose |
|------|---------|
| `000_auth_shim.sql` | Recreates `auth.uid()/auth.jwt()/auth.role()` and the `authenticated`/`anon` roles for non-Supabase Postgres |
| `001_workflow.sql` | Workflow / maker-checker helper definitions |
| `README.md` | Load order |

## Load order (non-Supabase deployments)
1. `db/000_auth_shim.sql` — the `auth.*` helpers the Portal SQL depends on.
2. The Portal migrations from `ficium-portal/supabase/migrations/` in timestamp
   order — schema, RLS, maker-checker — **loaded unchanged**.

On Supabase (SaaS) the platform already provides `auth.*`, so skip step 1 and
connect directly to the Supabase Postgres via the transaction pooler, bypassing
PostgREST.

---

## How tenant isolation is enforced at runtime
Per request, `app/core/db.py` `tenant_session()` opens a transaction and runs
`SELECT set_config('request.jwt.claims', '<verified claims>', true)`. Every query
then executes under the same RLS policies PostgREST would have triggered, scoped
to the caller's `institution_id`. The API holds no tenant-filtering logic of its
own — the database is the enforcement point.
