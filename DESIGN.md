# ficium-portal-api — Design Document

_Last updated: 24 June 2026_

## 1. Why this service exists (ADR-001)
To keep the database's RLS-based security model while removing the hard
dependency on Supabase's PostgREST. PostgREST is convenient but couples the
platform to Supabase; replacing it with a thin FastAPI shell that sets
`request.jwt.claims` itself makes the **entire data layer portable** to client
cloud or on-prem with zero schema rewrite. This is the single most important
design decision in the repo.

## 2. Thin shell, fat database
Business rules, tenancy, and maker-checker live in SQL (policies + SECURITY
DEFINER functions). The API verifies the token, sets the claims GUC, and calls
the SQL. Keeping logic in one place (the DB) avoids the classic drift between an
API's idea of authorisation and the database's.

## 3. Endpoint design mirrors the old Supabase calls
Each route maps 1:1 to a call the frontend previously made against
Supabase/PostgREST (see README's endpoint table). This made the migration a
swap, not a rewrite — the frontend changed a base URL, not its semantics.

## 4. Scope boundary: no marketplace cross-project reads
Marketplace requests/bids/products are owned by the Ficium App's Supabase
project and are read by the frontend directly. Routing them through this service
would couple the Portal to the App's storage, so they are deliberately excluded.

## 5. Connection model
psycopg2 over the Supabase transaction pooler (6543); no `SET ROLE` (the pooler
user can't switch roles and RLS doesn't need it — it checks `auth.uid()`).

## 6. Identity migration (ADR-002)
Tracks collapsing the ficium-auth vs Supabase `auth.users` identity spaces to
one source of truth. Until done, `sub` parity keeps RLS resolving correctly.

## Open items
- Complete ADR-002 identity unification.
- Migrate the remaining Portal admin/groups/users calls off direct Supabase onto
  this API (ARCHITECTURE stages 4c–5 in `ficium-portal`).
