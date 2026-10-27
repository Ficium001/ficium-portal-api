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

## How migrations run now (Oct 19 2026)

`ops.schema_migration` (039) records every file in this folder with its
SHA-256. Use the runner, on Supabase or any plain Postgres:

```
PORTAL_DB_DSN=... python scripts/migrate.py --check   # what CI runs
PORTAL_DB_DSN=... python scripts/migrate.py --apply   # apply pending, in order
```

Rules: a file is immutable once applied (an edit fails `--check`; fix forward
with a new number). Each file and its ledger row run in one implicit
transaction, so a failed file leaves nothing behind. Never apply SQL to a live
database without a file here: several 2026 hotfixes exist only in Supabase's
own history (e.g. `fix_portal_notification_deal_links`,
`enable_rls_marketplace_sync_state`) and would be missing from a rebuild.
The table below is history; the ledger is the source of truth.

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
| `021_ingest_app_request_drop_employer_allowlist.sql` | Removes the dead `employer` entry from `ingest_app_request()`'s Phase 1 allowlist (016's trigger already strips it) | Yes | ✅ (applied Oct 5 2026) |
| `022_schema_cleanup_step1.sql` | Retires `workflow` + `bid_notify` (renamed `_retired_*_20261006`, hard-drop ≥ 2026-11-06), folds `app` into `institution`. Rollback: `rollback/022_rollback.sql` | Yes | ✅ (applied Oct 6 2026) |
| `023_sync_loop_fix_ingest_app_request.sql` | Fixes the request-sync write loop: `closed`→`expired`, upsert on `id`, window-repair, skip no-op rewrites, allocations only when changed. Rollback: `rollback/023_rollback.sql` | Yes | ✅ (applied Oct 6 2026) |
| `024_admin_consolidation_phase1.sql` | Phase 1 of retiring `admin`/`identity`: portal_admin self-contained, policies + institution functions repointed, `commission_event` moved, staff group back-fill. Self-verifying. Rollback: `rollback/024_rollback.sql` | Yes | ✅ (applied Oct 6 2026) |
| `025_deal_booking.sql` | Booking hand-off: `marketplace.loan_booking` (tenant read-only via RLS + GRANT SELECT), `record_loan_booking()` (all rules + commission event; not executable by anon/authenticated), `deal_booked` notification kind, `inst:booking` granted to every group holding `inst:pipeline`. Dry-run verified in a rolled-back transaction (11 rule checks) | Yes | ✅ (applied Oct 16 2026) |
| `026_facility_approval.sql` | Credit approval of facilities: `facility` approval entity, `pipeline_stage_def.approval_route`, stage→approval link, trigger driving the deal from the engine outcome (approve→next stage, reject→declined, escalate→re-route, expire/withdraw→resubmit), `close_deal()` (decline/withdraw with reason), fixes `approvals_is_eligible()` (read non-existent `institution.user_role`), default "Credit approval" template + catch-all facility DoA rule per institution with a `board_approval` stage. Dry-run verified in a rolled-back transaction (approve, maker recusal, reject w/o comment, reject→declined, withdraw→active, close during approval, re-close, cross-institution) | Yes | ✅ (applied Oct 17 2026) |
| `027_document_checklist.sql` | Document checklist: `institution.doc_requirement` (per workflow, per stage, provided by, mandatory), `marketplace.deal_document` + `deal_document_event` (status required→received→verified / rejected / waived, full history), `ensure_deal_documents()`, `deal_document_act()` (four eyes on verify, notes on reject/waive, closed deals frozen, verified files not replaceable), `stage_documents_outstanding()` (stage gate). Starter checklist on `credit_docs` / lending `legal_review` stages. Tenant read-only + GRANT SELECT; functions not executable by anon/authenticated. Dry-run verified (13 checks) | Yes | ✅ (applied Oct 17 2026) |
| `028_structured_benefits.sql` | Benefits get a priced value (`value_amount` + `value_unit` pct_pa/pct/mur; CHECK uses IS NOT NULL so a NULL unit can't slip through), copied into `bid_benefit` snapshots; `_execute_action` edited in place (asserted regex edits): duplicate `bid_benefit` insert in `bid.submit` removed (trigger is the single snapshot path), `benefit.create/update` carry the value; validity-order and title-length CHECKs (NOT VALID). Dry-run verified | Yes | ✅ (applied Oct 17 2026) |
| `038_registration_and_admin_lockdown.sql` | **Critical security fix.** Self-registered users could insert themselves as active platform super_admin, as admin member of any institution, or create a pre-approved institution; also WORM audit-log forging and cross-tenant doc/notification updates. Locks registration to an unapproved institution + its first primary admin, removes client writes to `portal_admin.admin_users`/`admin_audit_log`, revokes stray `anon` grants. Self-verifying (replays every attack + the real registration path) | Yes | ✅ (applied Oct 19 2026; attacks re-run independently afterwards, all blocked) |
| `039_migration_ledger.sql` | `ops.schema_migration` ledger + `scripts/migrate.py` runner; CI `--check` gate. Portable (Supabase roles handled conditionally; RLS not forced so a non-BYPASSRLS owner can migrate). 000 recorded as skipped, 001–038 as baseline | Yes | ✅ (applied Oct 19 2026; live ledger verified against all 41 repo hashes) |
| `040_four_eyes_switch.sql` | Four-eyes switch per action per institution (Setup › Controls): `governance.four_eyes_catalog` (locked/default), `four_eyes_policy`, `governance.submit` choke point (off → executes as `control='single'`, audited, maker module checked), switch-off is a locked four-eyes action, switch-on immediate. Executor for `user.update/deactivate/reactivate` (previously unapprovable). Closes direct bid inserts and forged `governance.action` inserts/approvals. Self-verifying (12 checks on live MCB users, rolled back) | Yes | ✅ (applied Oct 19 2026) |
| `041_member_changes_only_through_four_eyes.sql` | Primary admin cannot be edited/deactivated (trigger on `governance.action`); bank users lose direct UPDATE/DELETE on `institution.member`. Self-verifying | Yes | ✅ (applied Oct 19 2026) |
| `042_benefits_through_four_eyes.sql` | Every benefit change goes through `governance.submit` (ordinary benefits and deactivations were direct writes); executor handles benefits itself (legacy one forced `is_guaranteed = true`); direct INSERT/UPDATE on `institution.benefit` revoked. Self-verifying | Yes | ✅ (applied Oct 19 2026) |
| `043_password_reset_four_eyes.sql` | `user.reset_password` is a locked four-eyes action (was one admin, who saw the password). Temp passwords (reset and new user) are released once, only to the approving checker: `governance.action.credential_released_at` claimed in the same transaction as the hash write. Primary admin excluded. Self-verifying | Yes | ✅ (applied Oct 19 2026) |
| `044_controls_approved_by_admins_only.sql` | Approving a `four_eyes.*` action requires an institution admin (040 allowed any non-maker). Verification SKIPPED at apply: every active MCB member is an admin, so the non-admin path is untested on live data until a non-admin test user exists | Yes | ✅ (applied Oct 19 2026, verification skipped) |
| `045_drop_retired_bid_notify.sql` | Drops `_retired_bid_notify_20261006` (pre-contract notifier, incl. an outbound HTTP function; no dependents). Verifies no Portal DB function makes outbound HTTP calls. `_retired_workflow_20261006` deliberately left: has tables, external deps and a trigger | Yes | ✅ (applied Oct 19 2026) |
| `046_accept_bid_guarded.sql` | **Data-protection fix found by the suite's first real run.** `accept_bid` did not lock or check the request: a second acceptance naming a different bid flipped the winner while the borrower's PII stayed with the first bank. Now: row lock, same-bid repeat is idempotent, different bid refused, only `bidding` requests and live bids accepted. Self-verifying | Yes | ✅ (applied Oct 21 2026) |
| `047_actor_refs_to_ficium_auth.sql` | Repoints `bid_event.actor_id`, `institution.suspended_by/compliance_reviewed_by`, `kyb_document.reviewed_by` from `auth.users` to `auth_portal.auth_users` (one identity system). All empty when written | Yes | ✅ (applied Oct 23 2026) |
| `048_z4_lockdown.sql` | Z4 lock-down: drops browser self-registration policies/helpers (038) and removes every `anon` grant on Ficium schemas; plus dashboard steps (disable sign-up, expose `auth_portal` only, network restrictions, delete retired edge function) | Yes | ✅ (applied Oct 23 2026, after Z4 shipped) |
| `049_onboarding_and_compliance_rule.sql` | Creates the never-existing onboarding prefs table (`institution.onboarding_pref`); one rule for required documents (`institution.required_doc_types`) used by the compliance view (which ignored `applies_to`, so nobody could ever be compliant); `applies_to` in the `institution_type` vocabulary. Regulator mapping confirmed by KJ | Yes | ✅ (applied Oct 23 2026) |
| `050_no_compliance_no_bids.sql` | Bank-grade gate: one BEFORE INSERT guard on `marketplace.bid` refuses bids unless `institution.compliance.can_bid` (covers four-eyes, autobid, API and any future path) | Yes | ✅ (applied Oct 23 2026) |
| `051_compliance_waiver.sql` | Per-institution, time-limited (max 180 days) waiver of the document gate; Ficium admins only, reason required, audited by the API. `can_bid` = approved and (documents complete or active waiver); approval/suspension never waivable | Yes | ✅ (applied Oct 23 2026) |
| `052_leasing_maps_to_vehicle_loan.sql` | Borrower-app Vehicle Loan is app type `leasing`, which fell through to `personal_loan` in `catalog.product_id_for_app_type()`. Adds `leasing → vehicle_loan`; self-verifying (aborts if any of 19 other mappings moves). Existing mis-filed request left as is | Yes | ✅ (applied Oct 24 2026) |
| `053_revoke_anon_definer_functions.sql` | Security audit: removes anon/PUBLIC EXECUTE from all 16 SECURITY DEFINER functions anon could call (accept_bid, advance_pipeline_stage, relist_request, ...); authenticated/service_role unchanged (self-verified). Guarded by test_no_definer_function_executable_by_anon | Yes | ✅ (applied Oct 24 2026) |
| `054_expire_stale_approvals.sql` | Expired approvals: sweep writes audit rows, partial index, pg_cron every 5 min (pg_cron installed on Portal DB), backfills rows stuck `pending` past `expires_at`. Reads also derive the expired state in SQL, so they never depend on the sweep | Yes | ✅ (applied Oct 27 2026) |
| `055_admin_command_channel.sql` | Admin site -> portal-api over HTTPS only: append-only `integration.admin_command_log` (idempotency by command_id, maker/checker), `institution.doc.reviewed_by_staff` (reviewer as ficium-auth user). No new DB login for the admin site | Yes | ⏳ not yet applied |

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

