# Ficium — Data Dictionary

_Generated 1 August 2026 from the live Supabase catalogs. Regenerate rather than hand-edit._

Ficium runs **two physically separate Postgres databases**. They are not replicas of
each other and they do not share a connection. Data moves in one direction only,
App DB → Portal DB, through the sync described in `ARCHITECTURE.md`.

| | App DB | Portal DB |
|---|---|---|
| Supabase project | `wixfhjlsjkiwfvqewvmt` | `egwobcajdlragubtkpqp` |
| Serves | Borrower app (`ficium`) | Institution portal + API (`ficium-portal`, `ficium-portal-api`) |
| Auth | Supabase Auth (`auth.users`, `auth.uid()`) | `ficium-auth` RS256 JWT (`auth_portal.auth_users`) |
| Business schemas | `public`, `finance`, `fico`, `admin` | `institution`, `marketplace`, `catalog`, `governance`, `identity`, `audit`, `portal_admin`, `admin`, `workflow`, `auth_portal`, `public` |
| Tables / columns | {{APP_T}} / {{APP_C}} | {{POR_T}} / {{POR_C}} |

## Reading this document

- **Null** — `yes` means the column is nullable.
- **Key / Notes** — `PK` for primary key, `FK <target>` for a foreign key, plus any
  column comment recorded in the catalog.
- Types are the Postgres types, abbreviated (`timestamptz` for
  `timestamp with time zone`, `char(n)` for `character(n)`).
- Views are excluded from the column tables and listed separately below.

## Cross-cutting conventions

1. **UUID primary keys** everywhere except three deliberate exceptions:
   `institution.approval_outbox` (bigint sequence, ordering matters),
   `marketplace.sync_state` (smallint, single-row singleton), and
   `public.kyc_settings` (integer, single-row singleton).
2. **`created_at` / `updated_at`** are `timestamptz NOT NULL DEFAULT now()`.
   `updated_at` is only trustworthy where a trigger maintains it — see the
   trigger list; several tables have the default but no trigger.
3. **Money** is `numeric`, never float. Precision varies by origin: App DB
   generally uses `numeric(15,2)`, the Portal marketplace uses `numeric(20,6)`.
   Rates are `numeric(8,4)` on the Portal side and `numeric(5,2)` on the App side.
4. **Currency** defaults to `MUR`; the Portal DB enforces it through
   `catalog.currency`, the App DB stores it as free text.
5. **Secrets are never stored in plaintext.** Tokens, API keys, passwords and
   webhook secrets are all persisted as hashes (`*_hash`, `key_hmac`,
   `password_hash`, `code_hash`).
6. **Audit tables are append-only.** `marketplace.bid_event` and `audit.event`
   have `block_mutation` triggers; the rest are protected by RLS with no
   UPDATE/DELETE policy.

## Two-phase identity model

This is the single most important rule in the schema and it is enforced in
several places at once.

- **Phase 1 (pre-acceptance).** Institutions see an anonymised request. No name,
  no email, no phone, no `auth.uid()`. `marketplace.request.consumer_id` is an
  opaque identifier and `consumer_ref` is a display token.
- **Phase 2 (post-acceptance).** Only when the borrower accepts a bid does
  `marketplace.bid_acceptance` receive the borrower's real identity, and only the
  winning institution can read that row.

Chat carries the same rule: `public.request_messages.sender_id` holds the
borrower's real `auth.uid()`, which is stable across every request they ever
make. The portal API's `_mask()` strips it on the institution read path. It must
never be exposed, even post-acceptance.

## App DB — enumerated types

| Type | Values |
|---|---|
| `product_type` | sme_loan, personal_loan, mortgage, fixed_deposit, savings_account, credit_card, business_account, investment_account, leasing, overdraft, business_loan, equities, unit_trust, savings_plan, government_bonds, offshore_investment, mixed_portfolio |
| `request_status` | open, closed, accepted, expired, rejected, awaiting_consent |
| `bid_status` | submitted, accepted, rejected, expired, withdrawn |
| `kyc_status` | pending, verified, rejected, pending_review, under_review |
| `user_role` | client, bank, admin |
| `user_type_enum` | individual, business |
| `title_type` | mr, mrs, ms, miss, dr, prof, other |
| `gender_type` | male, female, other, prefer_not_to_say |
| `id_document_type` | national_id, passport, drivers_license, other |
| `vault_doc_type` | nic, passport, birth_certificate, driving_licence, title_deed, valuation_report, land_registry_extract, payslip, employment_letter, tax_return, bank_statement, loan_statement, credit_card_statement, brn_certificate, audited_accounts, insurance_policy, marriage_certificate, other |
| `vault_extract_status` | pending, processing, extracted, attested, failed, manual_review |
| `couple_status` | pending_verification, verified, dissolved |
| `relationship_doc_status` | pending_ocr, verified, rejected |
| `relationship_match_status` | pending, both_matched, partial_match, no_match |
| `participant_role` | primary, co_applicant, guarantor |
| `liability_type` | joint_and_several, several, guarantor |
| `consent_state` | invited, consented, declined, revoked |
| `invitation_channel` | email, sms |
| `invitation_status` | pending, accepted, declined, expired, revoked |
| `notification_kind` | kyc_verified, kyc_rejected, request_created, request_expiring, bid_received, bid_accepted, bid_expired, system |
| `action_category_type` | institution.approve, institution.suspend, institution.modules_update, institution.deployment_change, onboarding.stage_advance, compliance.status_update, bid.submit, bid.withdraw, bid.accept, webhook.create, webhook.update, webhook.delete, api_key.create, api_key.revoke, user.invite, user.role_change, user.remove, sla.update, request.submit, request.cancel |
| `action_status_type` | pending, approved, rejected, expired, cancelled |
| `actor_type` | institution_user, client_user, ficium_admin, system |
| `outcome_type` | success, rejected, expired, error |
| `institution_type` | bank, fintech, micro_credit, insurance, investment_firm, other |
| `deployment_model_type` | saas, paas, on_prem |
| `integration_mode_type` | portal, webhook, api_pull, core_banking |
| `onboarding_stage_type` | registered, commercial_review, deployment_selected, modules_assigned, technical_setup, compliance_review, pending_approval, approved, suspended |
| `compliance_status_type` | not_submitted, under_review, passed, failed, expired |
| `plan_tier` | starter, pro, enterprise |
| `webhook_event_status_type` | pending, delivered, failed, retrying |

Legacy duplicates still present: `bid_status_type` and `request_status_type`
mirror `bid_status` / `request_status` from the pre-v2 schema. Neither is used by
current code. `product_type` is the one enum that must stay in lockstep with the
frontend `ProductType` union in `src/individual/requests/api/requests.ts` — they
have drifted before, and the failure mode is a runtime
`invalid input value for enum product_type` on request creation.

## App DB — triggers

| Table | Trigger | What it does |
|---|---|---|
| `requests` | `trg_requests_touch_updated_at` | Maintains `updated_at`. **Load-bearing** — the marketplace keyset sync is ordered on this column. |
| `requests` | `on_request_created` | Fans out notifications. |
| `requests` | `on_request_created_audit` | Writes the audit event. |
| `requests` | `trg_marketplace_sync` | Calls `marketplace_sync.on_request_change()` → HTTP kick to the portal API. |
| `request_messages` | `request_messages_enforce_trg` | Enforces the structured-vs-free chat rules in a `BEFORE` trigger. Required because `ficium-portal-api` connects with a service session that bypasses RLS. |
| `clients` | `on_client_kyc_change` | KYC status transitions and notifications. |
| `bid_acceptances` | `on_bid_accepted_audit` | Audit trail on acceptance. |
| `client_vault_document` | `trg_vault_extract`, `set_vault_doc_updated_at` | Kicks extraction; maintains `updated_at`. |
| `client_documents` | `trg_doc_readiness` | Recomputes goal/journey readiness. |
| `finance.accounts` / `finance.holdings` / `finance.holding_prices` | `recompute_on_*_change` | Upserts `finance.net_worth_history` via `recompute_snapshot()`. |

## App DB — row-level security

RLS is enabled on **every** table in `public`, `finance`, `fico` and `admin`.
Policy counts per table are listed in `SECURITY.md`. Two notes worth carrying:

- `public.request_decisions` has RLS enabled and **zero policies**, which makes
  it deny-all for `authenticated` and `anon`. It is reachable only through
  `SECURITY DEFINER` functions (`reject_request()`). That is intentional, but it
  is also indistinguishable from an oversight — if the design is deliberate it
  should carry a table comment saying so.
- The `finance` schema needs both `GRANT USAGE` on the schema and table-level DML
  grants in addition to policies, because its RPCs are `SECURITY INVOKER`.
  Missing grants surface as a 403 before RLS is ever evaluated.
