
---

# Portal DB (`egwobcajdlragubtkpqp`)

Eleven schemas, each with a distinct security boundary.

| Schema | Purpose |
|---|---|
| `catalog` | Platform reference data — products, families, currencies, countries, regulators, module keys. Read-mostly, shared by all tenants. |
| `institution` | Per-tenant configuration and workflow — members, groups, products, benefits, approvals, doc templates, webhooks, API keys. |
| `marketplace` | The transactional core — requests, bids, acceptances, loan pipelines. |
| `governance` | Cross-cutting maker-checker ledger. |
| `audit` | Append-only audit event stream. |
| `identity` | Current identity model — profiles, login events, MFA, IP allowlists. |
| `auth_portal` | `ficium-auth` service tables. Service-role only. |
| `portal_admin` | Ficium internal admin — users, roles, groups, sessions, dual control. |
| `admin` | Newer admin model plus commission events and notification log. Overlaps `portal_admin` (see below). |
| `workflow` | Generic workflow templates. Superseded by `institution.pipeline_*`; see below. |
| `public` | Portal notifications plus a one-off migration log. |

## Portal DB — enumerated types

| Type | Values |
|---|---|
| `institution.inst_role` | super_admin, admin, analyst, viewer, compliance, api_operator |
| `institution.stage_key_enum` | credit_docs, offer_letter, legal_review, board_approval, disbursement, custom |
| `marketplace.pipeline_status_enum` | active, completed, withdrawn, declined |
| `marketplace.stage_status_enum` | pending, active, awaiting_approval, completed, skipped, blocked |
| `portal_admin.action_risk` | low, medium, high, critical |
| `portal_admin.admin_user_status` | active, locked, suspended, pending_mfa, deactivated |
| `portal_admin.audit_outcome` | success, rejected, failed, blocked, expired, logged |
| `portal_admin.dual_control_status` | pending, approved, rejected, expired, cancelled, executed |

Note that `marketplace.request.status` and `marketplace.bid.status` are **`text`,
not enums**, guarded by CHECK constraints. This diverges from the App DB, where
the equivalents are true enums. Values in play: request `open | bidding |
accepted | cancelled | expired`; bid `submitted | accepted | rejected | expired |
withdrawn`.

## Portal DB — views

| View | Definition summary |
|---|---|
| `marketplace.my_bids` | Bids for the calling institution joined to request and product labels. Backs `GET /marketplace/my-bids`. |
| `institution.compliance` | Per-institution rollup: `is_approved`, `is_compliant`, `missing_docs[]`, `can_bid`. The `can_bid` gate is what blocks bidding for an incomplete tenant. |

## Portal DB — key stored procedures

Tenant scoping runs through `SECURITY DEFINER` helpers rather than being repeated
in each policy.

| Function | Role |
|---|---|
| `institution.current_member_ctx_v2()` | Resolves the calling JWT to a member row. The `_v2` suffix is live; the original is retained for older policies. |
| `institution.get_my_institution_id()` / `get_my_member_id()` | Tenant and actor resolution used throughout RLS. |
| `institution.has_module()` / `has_role()` / `is_active()` | Entitlement and RBAC checks. |
| `institution.approvals_route/cast/advance/evaluate_stage/withdraw` | The approval chain state machine. |
| `institution.submit_for_approval()` / `approve_action()` / `reject_action()` | Maker-checker lifecycle. |
| `marketplace.ingest_app_request()` | **The single entry point for App DB → Portal DB sync.** Maps app product types to `catalog.product`, upserts `marketplace.request` on `idempotency_key`, and copies an **allowlisted** set of phase-1 keys into `metadata`. Any new phase-1 field must be added to that allowlist or it is silently dropped. |
| `marketplace.accept_bid()` | Acceptance transaction: sets the winning bid, writes `bid_acceptance`, creates the pipeline. |
| `marketplace.create_pipeline_from_acceptance()` | Instantiates `loan_pipeline` + `pipeline_stage_instance` rows from the institution's template. |
| `marketplace.close_expired_windows()` | Expires requests whose bid window has closed. |
| `marketplace.guard_bid_window()` | Rejects bids outside the open window. |
| `marketplace.snapshot_bid_benefits()` | Freezes benefit text onto the bid so later edits don't rewrite history. |
| `catalog.product_id_for_app_type()` | Maps an App DB `product_type` label to a `catalog.product`. Previously fell back silently to `personal_loan`; that fallback was a real defect and is worth an explicit test. |
| `governance.submit()` / `expire_stale_actions()` | Maker-checker submission and TTL sweep. |
| `audit.log()` | Sole writer to `audit.event`. |

## Portal DB — RLS posture

Every `institution`, `marketplace`, `catalog`, `identity`, `governance`,
`workflow` and `portal_admin` table has RLS enabled. Three deliberate exceptions
and two worth reviewing:

| Table | State | Assessment |
|---|---|---|
| `auth_portal.*` (7 tables) | RLS on, **zero policies** | Correct. Deny-all to `authenticated`/`anon`; reached only by `ficium-auth` on the service role. |
| `marketplace.sync_state` | RLS off | Acceptable. Single-row cursor written only by the sync endpoint on the service role. Enabling RLS with no policy would be tighter and cost nothing. |
| `public._identity_migration_log` | RLS on, zero policies | Migration artefact. Safe to drop once the identity migration is closed out. |
| `admin.commission_event` | **RLS off, no policies** | Review. Holds per-deal revenue: deal amount, commission rate, invoice reference. If PostgREST exposes the `admin` schema this is readable by any authenticated caller. |
| `admin.notification_log` | **RLS off, no policies** | Review. Same exposure question, lower sensitivity. |

## Known schema debt

Recording these so they are decisions rather than surprises.

1. **`portal_admin` and `admin` overlap.** `portal_admin.admin_users` /
   `admin_roles` / `user_groups` / `admin_sessions` / `admin_audit_log` have
   near-equivalents in `admin.user` / `role` / `system_group` / `session`, and
   `identity.profile` overlaps both. Three generations of the same model are live
   at once. Application code currently reads `portal_admin`. Consolidating needs
   a data migration, not just a view.
2. **`workflow.*` is superseded.** `workflow.template` / `stage` /
   `stage_assignment` / `doc_requirement` predate
   `institution.pipeline_template` / `pipeline_stage_def`, which is what the
   pipeline module actually uses. The `workflow` tables are not referenced by
   `ficium-portal-api`. Confirm they are empty, then drop them.
3. **`institution.api_key` carries three key columns** — `key_hmac`,
   `key_prefix`, `key_hash`. Only the HMAC path is used for verification; the
   other two are migration residue.
4. **Credit-product lists are duplicated.** `_CREDIT_PRODUCT_TYPES` in
   `ficium-portal-api` and `CREDIT_PRODUCT_CODES` in `ficium-portal` are two
   hand-maintained copies of the same list, and they drive whether
   DSR/collateral/employment sections render. They should be derived from
   `catalog.product_family`.
5. **`marketplace.request.status` is text, `public.requests.status` is an enum**,
   and the value sets differ (`bidding` vs `closed` / `rejected` /
   `awaiting_consent`). The mapping lives inside `ingest_app_request()` and is
   not documented anywhere else in the schema.

