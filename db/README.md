# Database load order (non-Supabase deployments)

For client-cloud / on-prem / MRU-hosted Postgres, load in this order:

1. `000_auth_shim.sql` — recreates `auth.uid()/auth.jwt()/auth.role()` and the
   `authenticated`/`anon` roles that the Portal SQL depends on.
2. The Portal migrations (from `ficium-portal/supabase/migrations/`, in
   timestamp order) — schema, RLS policies, and the maker-checker functions,
   **loaded unchanged**.

On Supabase (SaaS) the platform already provides the `auth.*` helpers, so skip
step 1 and connect `ficium-portal-api` directly to the Supabase Postgres
(transaction pooler) — bypassing PostgREST.
