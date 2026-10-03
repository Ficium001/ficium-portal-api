# db/ — Portal DB migrations

These SQL files are applied to the **Portal (Institution) DB** (`egwobcajdlragubtkpqp`).

---

## ⚠️ Aug 3 2026 incident — read this first

Three of these files had been sitting in the repo, fully coded against by
both the backend and the frontend, **without ever being applied to the live
database**: `008_esign.sql`, `009_entitlements.sql`, `010_autobid.sql`. This
README's table below didn't list any of them, which is almost certainly why —
there was no checklist to catch the gap. Every endpoint in `esign.py`,
`entitlements.py`, and `autobid.py` (22 endpoints total) was failing with
"relation/schema does not exist" — a raw DB error with no `HTTPException`,
which (before the global exception handler was added) the browser showed only
as a CORS failure, hiding the real cause completely.

Found via a production screenshot of `/esign` failing, which prompted checking
every recently-added `db/*.sql` file against what's actually live. All three
are now applied, along with the `GRANT` statements each one omitted (RLS
policies alone don't grant access — see the doc-templates incident below).

**Before adding a new file here, apply it immediately and add a row to the
table below in the same change.** Don't let code and schema drift again.

## Files

| File | Purpose | Must run on Supabase? | Live as of Aug 3 2026? |
|---|---|---|---|
| `000_auth_shim.sql` | Recreates `auth.uid()`, `auth.jwt()`, `auth.role()` and the `authenticated`/`anon` roles for non-Supabase Postgres | No — skip on Supabase | n/a |
| `001_workflow.sql` | Workflow/maker-checker helper definitions | Yes | ✅ |
| `003_expiry_notify.sql` | Updates `marketplace.close_expired_windows()` to fire `request-expired` pg_net call on expiry | Yes | ✅ |
| `004_accept_bid_reveal.sql` | Updates `marketplace.accept_bid()` to include `rate`, `rate_type`, `amount_offered`, `term_months` in return payload | Yes | ✅ |
| `005_api_keys_risk_tier.sql` | API key risk tier column + helper | Yes | ✅ |
| `005_execute_action_benefit.sql` | `_execute_action()` benefit-category handling | Yes | ✅ |
| `006_pipeline_rls.sql` | RLS + FORCE on pipeline template/stage/instance tables | Yes | ✅ |
| `007_approval_engine.sql` | Approval chains: committees, templates, DoA rules, instances, delegation | Yes | ✅ |
| `008_esign.sql` | E-signature: envelopes, signers, hash-chained event log | Yes | ✅ (applied Aug 3 2026 — was missing) |
| `009_doc_templates.sql` | Document template designer: templates, versions, generations | Yes | ✅ (tables existed; the `authenticated` GRANTs were missing — fixed separately Aug 3 2026) |
| `009_entitlements.sql` | Module entitlement & metered-usage layer (`entitlements` schema) | Yes | ✅ (applied Aug 3 2026 — was missing) |
| `010_autobid.sql` | Auto-bid rules engine (`autobid` schema) | Yes | ✅ (applied Aug 3 2026 — was missing) |
| `011_integration.sql` | Integration contract v1: `integration` schema — outbox, inbox, delivery state. No client-role access; portal-api (as `postgres`) only | Yes | ✅ (applied Oct 1 2026 with smoke test; anon/authenticated/service_role verified to have no access) |
| `012_security_lockdown_exposed_functions.sql` | Revokes anonymous (and unneeded signed-in) EXECUTE on 53 SECURITY DEFINER functions in the exposed schemas; aborts unless exact counts match | Yes | ✅ (applied Oct 2 2026; 93/93 tables still readable; get_admin_metrics 200 → 401 over HTTPS) |
| `013_member_login_sync_and_anon_table_lockdown.sql` | Trigger: member.active → auth_users.is_active (deactivation really blocks login); removes all `anon` grants on `auth_portal` tables + 2 public tables | Yes | ✅ (applied Oct 2 2026; real-path trigger test passed; 92/92 tables still readable; anon probes 200 → 401) |
| `014_member_login_sync_email.sql` | Trigger also mirrors member.email → auth_users.email (unique-violation → HTTP 409); lets portal-api stop writing auth_portal from tenant sessions | Yes | ✅ (applied Oct 2 2026; real-path trigger test + provisioning SQL verified on live schema, rolled back) |
| `015_auth_portal_authenticated_least_privilege.sql` | `authenticated` loses all write grants on `auth_portal`; keeps only `auth_users(id, is_active)` for the member-list join | Yes | ✅ (applied Oct 2 2026 after the portal-api fix deployed; denials + join verified) |
| `016_drop_employer_from_marketplace_request.sql` | Scrubs `metadata.employer` (12 rows) and adds a guard trigger so it can never be stored again | Yes | ✅ (applied Oct 2 2026; history fingerprint unchanged) |
| `017_integration_request_shadow_and_ordered_claim.sql` | Ordered per-aggregate delivery fix; `integration.request_shadow` + parity views (step 3, shadow mode, nothing live reads it) | Yes | ✅ (applied Oct 2 2026; replayed the 13 live requests: 12 match, 1 real drift found) |
| `018_integration_bid_publisher.sql` | Step 4 stage A: bid payload builder (== today's readback), catalog -> borrower product mapping, coalescing publisher, backfill. No trigger attached | Yes | ✅ (applied Oct 3 2026; payload == readback 2/2; contract-valid) |
| `019_integration_bid_publisher_triggers.sql` | Step 4 stage B: deferred triggers on bid / bid_benefit / bid_allocation + backfill (bid events start, shadow mode) | Yes | ✅ (applied Oct 3 2026 after both sides deployed; 2/2 delivered and identical on the borrower side) |
| `020_integration_acceptance_log.sql` | Step 5: idempotency log for the acceptance call (endpoint OFF by default) | Yes | ✅ (applied Oct 3 2026) |

The `005_*` pair and the `009_*` pair share a numeric prefix because they
landed independently rather than by strict sequence — treat the prefix as a
rough chronological hint, not a strict order dependency, and check each
file's own header comment for what it actually requires.

---

## Load order (non-Supabase / on-prem)

1. `000_auth_shim.sql` — creates `auth.*` helpers the Portal SQL depends on
2. Portal migrations from `ficium-portal/supabase/migrations/` in order — schema, RLS, maker-checker
3. Every other file in this directory, in filename order

On Supabase, skip step 1 (Supabase already provides `auth.*`). Steps 2 and 3 always apply.

---

## The recurring gap: RLS without a GRANT

Every one of the three files fixed on Aug 3 2026 enabled RLS and wrote
policies, but **none of them included a `GRANT` statement** for the
`authenticated` role. RLS policies only get evaluated after the base
table-level privilege check passes — `ENABLE ROW LEVEL SECURITY` with no
`GRANT` is not "more secure," it's just broken for every legitimate caller
too. This has now caused three separate incidents (`finance` schema, 
`institution.doc_template`, and all three files above). **Every new migration
that adds a table needs an explicit `GRANT` line for whichever role actually
queries it (`authenticated` via `tenant_conn()`, or none at all if every
access goes through a `SECURITY DEFINER` function) — do not assume RLS alone
is sufficient.**

---

## How tenant isolation works at runtime

Per request, `app/core/db.py` `tenant_session()` runs:

```sql
SELECT set_config('request.jwt.claims', '<verified JWT claims JSON>', true);
```

Every subsequent query in that transaction resolves `auth.uid()` from the GUC — the same mechanism PostgREST uses. RLS policies are the enforcement point; the API holds no tenant-filtering logic of its own.

