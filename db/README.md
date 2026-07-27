# db/ — Portal DB migrations

These SQL files are applied to the **Portal (Institution) DB** (`egwobcajdlragubtkpqp`).

---

## Files

| File | Purpose | Must run on Supabase? |
|---|---|---|
| `000_auth_shim.sql` | Recreates `auth.uid()`, `auth.jwt()`, `auth.role()` and the `authenticated`/`anon` roles for non-Supabase Postgres | No — skip on Supabase |
| `001_workflow.sql` | Workflow/maker-checker helper definitions | Yes |
| `003_expiry_notify.sql` | Updates `marketplace.close_expired_windows()` to fire `request-expired` pg_net call on expiry | Yes |
| `004_accept_bid_reveal.sql` | Updates `marketplace.accept_bid()` to include `rate`, `rate_type`, `amount_offered`, `term_months` in return payload | Yes |
| `009_entitlements.sql` | Module entitlement & metered-usage layer (`entitlements` schema): catalog, per-institution entitlements, partitioned usage ledger, RLS | Yes |

---

## Load order (non-Supabase / on-prem)

1. `000_auth_shim.sql` — creates `auth.*` helpers the Portal SQL depends on
2. Portal migrations from `ficium-portal/supabase/migrations/` in order — schema, RLS, maker-checker
3. `001_workflow.sql` — workflow helpers
4. `003_expiry_notify.sql`
5. `004_accept_bid_reveal.sql`

On Supabase, skip step 1 (Supabase already provides `auth.*`). Steps 2–5 always apply.

---

## How tenant isolation works at runtime

Per request, `app/core/db.py` `tenant_session()` runs:

```sql
SELECT set_config('request.jwt.claims', '<verified JWT claims JSON>', true);
```

Every subsequent query in that transaction resolves `auth.uid()` from the GUC — the same mechanism PostgREST uses. RLS policies are the enforcement point; the API holds no tenant-filtering logic of its own.
