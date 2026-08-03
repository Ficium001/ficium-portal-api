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
| Tables / columns | 47 / 548 | 84 / 976 |

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

## Schema `public`

### `public.activity_events`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  |  |
| `event_type` | text | no |  |  |
| `title` | text | no |  |  |
| `body` | text | yes |  |  |
| `goal_id` | uuid | yes |  |  |
| `request_id` | uuid | yes |  | FK public.requests.id |
| `meta` | jsonb | yes | `'{}'` |  |
| `read_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.app_features`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `key` | text | no |  | PK |
| `label` | text | no |  |  |
| `description` | text | no | `''` |  |
| `active` | boolean | no | `false` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.asset_details`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `user_id` | uuid | no |  |  |
| `savings` | numeric | yes | `0` |  |
| `investments` | numeric | yes | `0` |  |
| `property_value` | numeric | yes | `0` |  |
| `vehicle_value` | numeric | yes | `0` |  |
| `business_assets` | numeric | yes | `0` |  |
| `other_assets` | numeric | yes | `0` |  |
| `total_net_worth` | numeric | yes |  |  |
| `created_at` | timestamptz | yes | `now()` |  |

### `public.audit_events`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | yes |  | FK public.clients.id |
| `pending_action_id` | uuid | yes |  |  |
| `actor_id` | uuid | no |  |  |
| `actor_type` | text | no | `'client_user'` |  |
| `actor_role` | text | yes |  |  |
| `actor_ip` | inet | yes |  |  |
| `actor_device` | text | yes |  |  |
| `action_category` | text | no |  |  |
| `event_label` | text | no |  |  |
| `resource_type` | text | yes |  |  |
| `resource_id` | uuid | yes |  |  |
| `state_before` | jsonb | yes |  |  |
| `state_after` | jsonb | yes |  |  |
| `outcome` | text | no | `'success'` |  |
| `outcome_note` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.audit_logs`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `uuid_generate_v4()` | PK |
| `actor_id` | uuid | yes |  |  |
| `actor_type` | text | no | `'system'` |  |
| `institution_id` | uuid | yes |  |  |
| `event_type` | text | no |  |  |
| `resource_id` | uuid | yes |  |  |
| `resource_type` | text | yes |  |  |
| `diff` | jsonb | yes |  |  |
| `ip_address` | inet | yes |  |  |
| `user_agent` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.bid_acceptances`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `uuid_generate_v4()` | PK |
| `bid_id` | uuid | no |  |  |
| `request_id` | uuid | no |  |  |
| `client_id` | uuid | no |  |  |
| `institution_id` | uuid | no |  |  |
| `los_reference` | text | yes |  |  |
| `crm_reference` | text | yes |  |  |
| `core_banking_ref` | text | yes |  |  |
| `disbursement_status` | text | yes | `'pending'` |  |
| `accepted_at` | timestamptz | no | `now()` |  |

### `public.client_documents`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  |  |
| `journey_id` | uuid | yes |  |  |
| `type` | text | no |  |  |
| `label` | text | no |  |  |
| `storage_path` | text | no |  |  |
| `file_name` | text | no |  |  |
| `file_size` | integer | yes |  |  |
| `mime_type` | text | yes |  |  |
| `extracted` | jsonb | yes |  |  |
| `verified` | boolean | no | `false` |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.client_dossier`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  | FK public.clients.id |
| `employment_status` | text | yes |  |  |
| `employer_name` | text | yes |  |  |
| `monthly_income` | numeric(15,2) | yes |  |  |
| `additional_income` | numeric(15,2) | yes |  |  |
| `total_net_worth` | numeric(15,2) | yes |  |  |
| `has_existing_loans` | boolean | yes | `false` |  |
| `pep_declaration` | boolean | yes | `false` |  |
| `tax_residency` | text | yes |  |  |
| `source_of_wealth` | text | yes |  |  |
| `health_score` | integer | yes |  |  |
| `risk_score` | integer | yes |  |  |
| `affordability_score` | integer | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |
| `dependants` | integer | yes | `0` |  |

### `public.client_financial_snapshot`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  |  |
| `cash_savings` | numeric(15,2) | no | `0` |  |
| `fixed_deposits` | numeric(15,2) | no | `0` |  |
| `investments_value` | numeric(15,2) | no | `0` |  |
| `property_value` | numeric(15,2) | no | `0` |  |
| `vehicle_value` | numeric(15,2) | no | `0` |  |
| `other_assets` | numeric(15,2) | no | `0` |  |
| `mortgage_balance` | numeric(15,2) | no | `0` |  |
| `personal_loan_balance` | numeric(15,2) | no | `0` |  |
| `credit_card_balance` | numeric(15,2) | no | `0` |  |
| `vehicle_loan_balance` | numeric(15,2) | no | `0` |  |
| `other_liabilities` | numeric(15,2) | no | `0` |  |
| `monthly_income` | numeric(12,2) | no | `0` |  |
| `monthly_expenses` | numeric(12,2) | no | `0` |  |
| `monthly_loan_payments` | numeric(12,2) | no | `0` |  |
| `monthly_savings` | numeric(12,2) | no | `0` |  |
| `total_assets` | numeric(15,2) | yes |  |  |
| `total_liabilities` | numeric(15,2) | yes |  |  |
| `net_worth` | numeric(15,2) | yes |  |  |
| `debt_to_income_ratio` | numeric(5,2) | yes |  |  |
| `currency` | text | no | `'MUR'` |  |
| `snapshot_date` | date | no | `CURRENT_DATE` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |
| `income_verified` | boolean | yes | `false` |  |
| `income_verified_at` | timestamptz | yes |  |  |
| `income_verified_source` | text | yes |  |  |
| `property_verified` | boolean | yes | `false` |  |
| `property_verified_at` | timestamptz | yes |  |  |
| `liabilities_verified` | boolean | yes | `false` |  |
| `liabilities_verified_at` | timestamptz | yes |  |  |

### `public.client_loan_details`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  | FK public.clients.id |
| `loan_type` | text | yes |  |  |
| `outstanding_amount` | numeric(15,2) | yes |  |  |
| `monthly_repayment` | numeric(15,2) | yes |  |  |
| `bank_name` | text | yes |  |  |
| `remaining_months` | integer | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.client_vault_access_log`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `document_id` | uuid | no |  | FK public.client_vault_document.id |
| `client_id` | uuid | no |  |  |
| `action` | text | no |  |  |
| `actor_id` | uuid | yes |  |  |
| `ip_address` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.client_vault_document`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  | FK public.clients.id |
| `doc_type` | vault_doc_type | no |  |  |
| `storage_path` | text | no |  |  |
| `file_name` | text | no |  |  |
| `file_size_bytes` | integer | yes |  |  |
| `mime_type` | text | yes |  |  |
| `extract_status` | vault_extract_status | no | `'pending'` |  |
| `extract_job_id` | text | yes |  |  |
| `extracted_at` | timestamptz | yes |  |  |
| `attested_at` | timestamptz | yes |  |  |
| `extract_error` | text | yes |  |  |
| `extract_raw` | jsonb | yes |  |  |
| `confidence` | numeric(4,3) | yes |  |  |
| `doc_date` | date | yes |  |  |
| `doc_ref` | text | yes |  |  |
| `expires_at` | date | yes |  |  |
| `retain_until` | date | yes |  |  |
| `deleted_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.client_vault_property`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  | FK public.clients.id |
| `deed_document_id` | uuid | yes |  | FK public.client_vault_document.id |
| `valuation_doc_id` | uuid | yes |  | FK public.client_vault_document.id |
| `address` | text | yes |  |  |
| `land_area_sqm` | numeric | yes |  |  |
| `property_type` | text | yes |  |  |
| `registered_owner` | text | yes |  |  |
| `deed_date` | date | yes |  |  |
| `deed_ref` | text | yes |  |  |
| `market_value` | numeric | yes |  |  |
| `valuation_date` | date | yes |  |  |
| `valuer_name` | text | yes |  |  |
| `valuation_currency` | text | yes | `'MUR'` |  |
| `is_mortgaged` | boolean | yes | `false` |  |
| `mortgage_lender` | text | yes |  |  |
| `mortgage_balance` | numeric | yes |  |  |
| `verified` | boolean | yes | `false` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.clients`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `email` | text | no |  |  |
| `full_name` | text | no | `''` |  |
| `first_name` | text | yes |  |  |
| `middle_name` | text | yes |  |  |
| `last_name` | text | yes |  |  |
| `title` | title_type | yes |  |  |
| `phone` | text | yes |  |  |
| `user_type` | text | no | `'individual'` |  |
| `company_name` | text | yes |  |  |
| `company_registration` | text | yes |  |  |
| `kyc_status` | kyc_status | no | `'pending'` |  |
| `date_of_birth` | date | yes |  |  |
| `gender` | text | yes |  |  |
| `address_line_1` | text | yes |  |  |
| `address_line_2` | text | yes |  |  |
| `city` | text | yes |  |  |
| `postal_code` | text | yes |  |  |
| `country` | text | yes | `'MU'` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |
| `kyc_submitted_at` | timestamptz | yes | `now()` |  |
| `admin_review_note` | text | yes |  |  |
| `reviewed_by` | text | yes |  |  |
| `reviewed_at` | timestamptz | yes |  |  |
| `nationality` | text | yes |  |  |
| `residence_status` | text | yes |  |  |
| `dependants` | integer | yes | `0` |  |

### `public.compliance_details`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `user_id` | uuid | no |  |  |
| `source_of_wealth` | text | yes |  |  |
| `source_of_wealth_other` | text | yes |  |  |
| `is_pep` | boolean | yes | `false` |  |
| `pep_details` | text | yes |  |  |
| `tax_residency` | text | yes | `'MU'` |  |
| `has_credit_issues` | boolean | yes | `false` |  |
| `missed_repayments` | boolean | yes | `false` |  |
| `blacklisted` | boolean | yes | `false` |  |
| `bankruptcy` | boolean | yes | `false` |  |
| `legal_disputes` | boolean | yes | `false` |  |
| `enhanced_due_diligence_required` | boolean | yes | `false` |  |
| `created_at` | timestamptz | yes | `now()` |  |

### `public.couple_link`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_a_id` | uuid | no |  | FK public.clients.id |
| `client_b_id` | uuid | no |  | FK public.clients.id |
| `status` | couple_status | no | `'pending_verification'` |  |
| `initiated_by_client_id` | uuid | no |  | FK public.clients.id |
| `verified_at` | timestamptz | yes |  |  |
| `dissolved_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.couple_relationship_document`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `couple_link_id` | uuid | no |  | FK public.couple_link.id |
| `vault_document_id` | uuid | no |  | FK public.client_vault_document.id |
| `uploaded_by_client_id` | uuid | no |  | FK public.clients.id |
| `doc_type` | vault_doc_type | no | `'marriage_certificate'` |  |
| `extracted_text` | text | yes |  |  |
| `name_a_matched` | boolean | no | `false` |  |
| `name_b_matched` | boolean | no | `false` |  |
| `match_score_a` | numeric | yes |  |  |
| `match_score_b` | numeric | yes |  |  |
| `match_status` | relationship_match_status | no | `'pending'` |  |
| `verification_status` | relationship_doc_status | no | `'pending_ocr'` |  |
| `reject_reason` | text | yes |  |  |
| `reviewed_by` | uuid | yes |  | FK public.clients.id |
| `reviewed_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.employment_details`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `user_id` | uuid | no |  |  |
| `employer_name` | text | yes |  |  |
| `industry` | text | yes |  |  |
| `job_title` | text | yes |  |  |
| `years_of_employment` | numeric | yes |  |  |
| `employment_type` | text | yes |  |  |
| `work_email` | text | yes |  |  |
| `employer_address` | text | yes |  |  |
| `business_name` | text | yes |  |  |
| `brn_number` | text | yes |  |  |
| `years_in_business` | numeric | yes |  |  |
| `average_monthly_revenue` | numeric | yes |  |  |
| `business_address` | text | yes |  |  |
| `tax_registration_number` | text | yes |  |  |
| `company_type` | text | yes |  |  |
| `number_of_employees` | integer | yes |  |  |
| `annual_revenue` | numeric | yes |  |  |
| `primary_profession` | text | yes |  |  |
| `primary_clients_region` | text | yes |  |  |
| `portfolio_website` | text | yes |  |  |
| `pension_income` | numeric | yes |  |  |
| `other_income_sources` | text | yes |  |  |
| `institution_name` | text | yes |  |  |
| `sponsor_type` | text | yes |  |  |
| `monthly_allowance` | numeric | yes |  |  |
| `part_time_employment` | boolean | yes | `false` |  |
| `created_at` | timestamptz | yes | `now()` |  |

### `public.kyc_scan_attempts`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `ip_hash` | text | no |  |  |
| `client_id` | uuid | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.kyc_settings`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | integer | no | `1` | PK |
| `ai_analysis` | boolean | no | `true` |  |
| `face_match` | boolean | no | `true` |  |
| `duplicate_face` | boolean | no | `true` |  |
| `ocr_name_match` | boolean | no | `true` |  |
| `proof_of_address` | boolean | no | `true` |  |
| `velocity_check` | boolean | no | `true` |  |
| `document_reuse` | boolean | no | `true` |  |
| `liveness_check` | boolean | no | `true` |  |
| `mrz_validation` | boolean | no | `true` |  |
| `permit_check` | boolean | no | `true` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.kyc_submissions`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | yes |  | FK public.clients.id |
| `submitted_at` | timestamptz | yes | `now()` |  |
| `provider` | text | yes |  |  |
| `reference_id` | text | yes |  |  |
| `risk_score` | integer | yes |  |  |
| `status` | text | yes |  |  |
| `flags` | text[] | yes |  |  |
| `id_document_path` | text | yes |  |  |
| `selfie_path` | text | yes |  |  |
| `proof_of_address_path` | text | yes |  |  |
| `document_type` | text | yes |  |  |
| `document_number` | text | yes |  |  |
| `reviewed_by` | uuid | yes |  |  |
| `reviewed_at` | timestamptz | yes |  |  |
| `review_note` | text | yes |  |  |
| `details` | jsonb | yes |  |  |
| `mrz_valid` | boolean | yes |  |  |
| `face_match_score` | numeric(5,2) | yes |  |  |
| `name_match_score` | integer | yes |  |  |
| `document_expired` | boolean | yes |  |  |
| `nationality` | text | yes |  |  |
| `residence_status` | text | yes |  |  |
| `same_nationality_residence` | boolean | yes | `true` |  |
| `permit_path` | text | yes |  |  |

### `public.market_data`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `ticker_id` | text | no |  |  |
| `value` | numeric | no |  |  |
| `display_value` | text | no |  |  |
| `change_pct` | numeric | no | `0` |  |
| `direction` | text | no |  |  |
| `history` | numeric[] | no | `'{}'` |  |
| `source` | text | no |  |  |
| `fetched_at` | timestamptz | no | `now()` |  |

### `public.market_deposit_rates`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `bank_name` | text | no |  |  |
| `bank_color` | text | no | `'#64748b'` |  |
| `rate_1y` | text | no |  |  |
| `rate_2y` | text | no |  |  |
| `rate_3y` | text | no |  |  |
| `fetched_at` | timestamptz | no | `now()` |  |

### `public.market_fx_rates`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `currency_code` | text | no |  |  |
| `currency_pair` | text | no |  |  |
| `bank_name` | text | no |  |  |
| `buy_rate` | numeric | no |  |  |
| `sell_rate` | numeric | no |  |  |
| `fetched_at` | timestamptz | no | `now()` |  |
| `rate_basis` | text | no | `'indicative'` |  |

### `public.market_lending_rates`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `product` | text | no |  |  |
| `icon_name` | text | no | `'landmark'` |  |
| `best_rate` | text | no |  |  |
| `is_best` | boolean | no | `false` |  |
| `fetched_at` | timestamptz | no | `now()` |  |

### `public.market_news`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `headline` | text | no |  |  |
| `category` | text | no |  |  |
| `emoji` | text | no |  |  |
| `plain_english` | text | no |  |  |
| `published_at` | timestamptz | no | `now()` |  |
| `related_ticker_id` | text | yes |  |  |
| `source` | text | no | `'manual'` |  |
| `body` | text | yes |  |  |
| `scope` | text | no | `'local'` |  |
| `source_name` | text | yes |  |  |
| `source_url` | text | yes |  |  |
| `content_hash` | text | yes |  |  |

### `public.market_preferences`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `user_id` | uuid | no |  | PK |
| `categories` | text[] | no | `'{}'` |  |
| `currencies` | text[] | no | `'{}'` |  |
| `scopes` | text[] | no | `'{local,global}'` |  |
| `default_mode` | text | no | `'everyday'` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.market_stories`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `story_key` | text | no |  |  |
| `category` | text | no |  |  |
| `emoji` | text | no |  |  |
| `related_cta` | boolean | no | `false` |  |
| `headline_everyday` | text | no |  |  |
| `plain_everyday` | text | no |  |  |
| `headline_finance` | text | no |  |  |
| `plain_finance` | text | no |  |  |
| `generated_at` | timestamptz | no | `now()` |  |
| `detail_everyday` | text | no | `''` |  |
| `detail_finance` | text | no | `''` |  |

### `public.notifications`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `user_id` | uuid | no |  |  |
| `kind` | text | no |  |  |
| `title` | text | no |  |  |
| `body` | text | yes |  |  |
| `link` | text | yes |  |  |
| `read_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `metadata` | jsonb | yes | `'{}'` |  |

### `public.request_allocations`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK public.requests.id |
| `product_type` | product_type | no |  |  |
| `amount` | numeric | yes |  |  |
| `sort_order` | integer | no | `0` |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.request_decisions`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK public.requests.id |
| `institution_id` | uuid | no |  |  |
| `decision` | text | no |  |  |
| `reason` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.request_invitation`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK public.requests.id |
| `inviter_client_id` | uuid | no |  | FK public.clients.id |
| `invited_email` | text | no |  |  |
| `invited_phone` | text | yes |  |  |
| `channel` | invitation_channel | no | `'email'` |  |
| `invited_client_id` | uuid | yes |  | FK public.clients.id |
| `proposed_role` | participant_role | no | `'co_applicant'` |  |
| `proposed_liability` | liability_type | yes |  |  |
| `proposed_ownership_bps` | integer | yes |  |  |
| `token_hash` | bytea | no |  |  |
| `status` | invitation_status | no | `'pending'` |  |
| `expires_at` | timestamptz | no | `(now() + '7 days')` |  |
| `sent_at` | timestamptz | no | `now()` |  |
| `responded_at` | timestamptz | yes |  |  |
| `revoked_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.request_message_template`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `code` | text | no |  | PK |
| `sender_type` | text | no |  |  |
| `label` | text | no |  |  |
| `body_template` | text | no |  |  |
| `params_schema` | jsonb | no | `'{}'` |  |
| `sort_order` | smallint | no | `100` |  |
| `is_active` | boolean | no | `true` |  |
| `created_at` | timestamptz | no | `now()` |  |

### `public.request_messages`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK public.requests.id |
| `sender_type` | text | no |  |  |
| `sender_id` | uuid | no |  |  |
| `body` | text | no |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `institution_id` | uuid | yes |  | The lender this thread is with. One thread per (request_id, institution_id) - NOT per bid, so a withdrawn/resubmitted bid does not fork or lose the conversation. |
| `kind` | text | no | `'structured'` | structured = template-driven (pre-acceptance, both sides). free = free text, permitted only for the winning lender after acceptance. |
| `template_code` | text | yes |  | FK public.request_message_template.code |
| `params` | jsonb | no | `'{}'` |  |

### `public.request_participant`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK public.requests.id |
| `client_id` | uuid | no |  | FK public.clients.id |
| `role` | participant_role | no | `'co_applicant'` |  |
| `liability_type` | liability_type | yes |  |  |
| `ownership_bps` | integer | yes |  |  |
| `consent_state` | consent_state | no | `'invited'` |  |
| `is_initiator` | boolean | no | `false` |  |
| `consented_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `public.requests`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `client_id` | uuid | no |  |  |
| `product_type` | product_type | no |  |  |
| `amount` | numeric(14,2) | no |  |  |
| `purpose` | text | yes |  |  |
| `preferred_term_months` | integer | yes |  |  |
| `max_rate` | numeric(5,2) | yes |  |  |
| `decision_deadline` | timestamptz | yes |  |  |
| `status` | request_status | no | `'open'` |  |
| `anonymized_brief` | text | yes |  |  |
| `accepted_bid_id` | uuid | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |
| `allocation_mode` | text | yes |  |  |
| `product_answers` | jsonb | no | `'{}'` | Raw key/value answers from the product-specific intake question flow (NewRequest.tsx PRODUCT_QUESTIONS), excluding __amount/__term internal keys. Shape varies by product_type; consumers should treat keys as optional. |

## Schema `admin`

### `admin.admin_users`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `uuid_generate_v4()` | PK |
| `user_id` | uuid | no |  | FK auth.users |
| `name` | text | no |  |  |
| `email` | text | no |  |  |
| `role` | text | no | `'staff'` |  |
| `active` | boolean | no | `true` |  |
| `last_login` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `admin.audit_events`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `admin_id` | uuid | yes |  |  |
| `institution_id` | uuid | yes |  |  |
| `actor_id` | uuid | no |  |  |
| `actor_type` | text | no | `'ficium_admin'` |  |
| `actor_ip` | inet | yes |  |  |
| `actor_device` | text | yes |  |  |
| `action_category` | text | no |  |  |
| `event_label` | text | no |  |  |
| `resource_type` | text | yes |  |  |
| `resource_id` | uuid | yes |  |  |
| `state_before` | jsonb | yes |  |  |
| `state_after` | jsonb | yes |  |  |
| `outcome` | text | no | `'success'` |  |
| `outcome_note` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `admin.platform_config`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `uuid_generate_v4()` | PK |
| `key` | text | no |  |  |
| `value` | jsonb | no |  |  |
| `description` | text | yes |  |  |
| `updated_by` | uuid | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |


## Schema `fico`

### `fico.conversation`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `user_id` | uuid | no |  |  |
| `started_at` | timestamptz | no | `now()` |  |
| `ended_at` | timestamptz | yes |  |  |
| `summarized` | boolean | no | `false` | rolled into profile.rolling_summary |

### `fico.message`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `conversation_id` | uuid | no |  | FK fico.conversation |
| `user_id` | uuid | no |  |  |
| `role` | text | no |  | user / assistant |
| `content` | text | no |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `fico.message_meter`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `user_id` | uuid | no |  | PK (user_id, period) |
| `period` | date | no |  | monthly quota bucket |
| `messages_used` | integer | no | `0` |  |

### `fico.profile`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `user_id` | uuid | no |  | PK |
| `rolling_summary` | text | no | `''` | Haiku-generated |
| `tone_notes` | text | no | `''` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |


## Schema `finance`

### `finance.accounts`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `user_id` | uuid | no |  |  |
| `institution_name` | text | no |  |  |
| `account_type` | text | no |  |  |
| `currency` | text | no | `'MUR'` |  |
| `balance` | numeric | no | `0` |  |
| `notes` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `finance.holdings`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `user_id` | uuid | no |  |  |
| `asset_type` | text | no |  | equity / crypto / fund |
| `symbol` | text | no |  |  |
| `exchange` | text | yes |  |  |
| `quantity` | numeric | no | `0` |  |
| `cost_basis` | numeric | yes |  |  |
| `currency` | text | no | `'USD'` |  |
| `notes` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `finance.holding_prices`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `symbol` | text | no |  | PK (symbol, exchange) |
| `exchange` | text | no | `''` |  |
| `asset_type` | text | no |  |  |
| `price` | numeric | no |  | Finnhub / CoinGecko |
| `currency` | text | no |  |  |
| `fetched_at` | timestamptz | no | `now()` |  |

### `finance.net_worth_history`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `user_id` | uuid | no |  | PK (user_id, snapshot_date) |
| `snapshot_date` | date | no | `CURRENT_DATE` |  |
| `cash_savings` | numeric | no | `0` |  |
| `fixed_deposits` | numeric | no | `0` |  |
| `investments_value` | numeric | no | `0` |  |
| `total_assets` | numeric | no | `0` |  |
| `total_liabilities` | numeric | no | `0` |  |
| `net_worth` | numeric | no | `0` |  |
| `currency` | text | no | `'MUR'` |  |
| `captured_at` | timestamptz | no | `now()` |  |

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


## Schema `catalog`

### `catalog.country`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `code` | char(2) | no |  | PK |
| `name` | text | no |  |  |
| `currency` | char(3) | no |  |  |
| `active` | boolean | no | `true` |  |

### `catalog.currency`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `code` | char(3) | no |  | PK |
| `name` | text | no |  |  |
| `symbol` | text | no |  |  |
| `decimal_places` | integer | no | `2` |  |

### `catalog.module`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `key` | text | no |  | PK |
| `label` | text | no |  |  |
| `description` | text | no | `''` |  |
| `side` | text | no | `'institution'` |  |
| `icon` | text | yes |  |  |
| `path` | text | yes |  |  |
| `sort_order` | integer | no | `0` |  |
| `active` | boolean | no | `true` |  |

### `catalog.product`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `family_id` | uuid | no |  | FK catalog.product_family |
| `code` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no | `''` |  |
| `currency` | char(3) | no | `'MUR'` | FK catalog.currency |
| `min_amount` | numeric(20,6) | yes |  |  |
| `max_amount` | numeric(20,6) | yes |  |  |
| `min_term_months` | integer | yes |  |  |
| `max_term_months` | integer | yes |  |  |
| `active` | boolean | no | `true` |  |
| `sort_order` | integer | no | `0` |  |
| `metadata` | jsonb | no | `'{}'` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `catalog.product_document`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `product_id` | uuid | no |  | FK catalog.product |
| `doc_key` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no | `''` |  |
| `required` | boolean | no | `true` |  |
| `allowed_mime_types` | text[] | no | `'{}'` |  |
| `max_size_bytes` | integer | yes |  |  |
| `sort_order` | integer | no | `0` |  |

### `catalog.product_eligibility`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `product_id` | uuid | no |  | FK catalog.product |
| `country` | char(2) | yes |  | FK catalog.country |
| `rules` | jsonb | no | `'{}'` |  |
| `description` | text | no | `''` |  |
| `active` | boolean | no | `true` |  |

### `catalog.product_family`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `code` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no | `''` |  |
| `icon` | text | yes |  |  |
| `sort_order` | integer | no | `0` |  |
| `active` | boolean | no | `true` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `catalog.product_parameter`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `product_id` | uuid | no |  | FK catalog.product |
| `key` | text | no |  |  |
| `label` | text | no |  |  |
| `data_type` | text | no | `'text'` |  |
| `required` | boolean | no | `false` |  |
| `options` | jsonb | yes |  |  |
| `validation` | jsonb | yes |  |  |
| `sort_order` | integer | no | `0` |  |

### `catalog.product_rate_model`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `product_id` | uuid | no |  | FK catalog.product |
| `rate_type` | text | no | `'fixed'` |  |
| `min_rate` | numeric(8,4) | yes |  |  |
| `max_rate` | numeric(8,4) | yes |  |  |
| `rate_unit` | text | no | `'percent_per_annum'` |  |
| `compounding` | text | no | `'monthly'` |  |
| `config` | jsonb | no | `'{}'` |  |

### `catalog.product_sla`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `product_id` | uuid | no |  | FK catalog.product |
| `bid_window_minutes` | integer | no | `240` |  |
| `auto_withdraw_minutes` | integer | no | `300` |  |
| `min_bids_required` | integer | no | `1` |  |
| `max_bids_allowed` | integer | yes |  |  |

### `catalog.regulator`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `code` | text | no |  | PK |
| `name` | text | no |  |  |
| `country` | text | no |  |  |
| `website` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |


## Schema `institution`

### `institution.api_key`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `label` | text | no |  |  |
| `key_hmac` | text | no |  |  |
| `key_prefix` | text | no | `''` |  |
| `key_hash` | text | no | `''` |  |
| `scopes` | text[] | no | `'{}'` |  |
| `active` | boolean | no | `false` |  |
| `mc_status` | text | no | `'pending_approval'` |  |
| `created_by` | uuid | yes |  |  |
| `requested_by` | uuid | yes |  |  |
| `approved_by` | uuid | yes |  |  |
| `approved_at` | timestamptz | yes |  |  |
| `rejection_note` | text | yes |  |  |
| `last_used_at` | timestamptz | yes |  |  |
| `last_used_ip` | inet | yes |  |  |
| `expires_at` | timestamptz | yes |  |  |
| `revoked_at` | timestamptz | yes |  |  |
| `revoked_by` | uuid | yes |  | FK institution.member |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.approval_action`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `stage_instance_id` | uuid | no |  | FK institution.approval_stage_instance |
| `actor_id` | uuid | no |  |  |
| `acting_as` | uuid | yes |  | delegation source |
| `action` | text | no |  | approve/reject/abstain |
| `comment` | text | yes |  |  |
| `checklist_state` | jsonb | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.approval_committee`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  |  |
| `name` | text | no |  |  |
| `description` | text | yes |  |  |
| `quorum_type` | text | no |  |  |
| `quorum_value` | numeric | yes |  |  |
| `tie_break` | text | no | `'chair'` |  |
| `allow_abstain` | boolean | no | `true` |  |
| `status` | text | no | `'active'` |  |
| `created_by` | uuid | no |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.approval_delegation`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  |  |
| `from_member` | uuid | no |  |  |
| `to_member` | uuid | no |  |  |
| `scope` | text | no | `'all'` |  |
| `reason` | text | no |  |  |
| `valid_from` | timestamptz | no |  |  |
| `valid_to` | timestamptz | no |  |  |
| `approved_by` | uuid | no |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.approval_instance`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  |  |
| `template_id` | uuid | no |  | FK institution.approval_template |
| `template_version` | integer | no |  | frozen at route time |
| `doa_rule_id` | uuid | no |  | FK institution.doa_rule |
| `entity_type` | text | no |  |  |
| `entity_id` | uuid | no |  |  |
| `entity_maker_id` | uuid | no |  | maker-checker exclusion |
| `entity_snapshot` | jsonb | no |  |  |
| `current_seq` | integer | no | `1` |  |
| `status` | text | no | `'in_progress'` |  |
| `withdraw_reason` | text | yes |  |  |
| `started_at` | timestamptz | no | `now()` |  |
| `resolved_at` | timestamptz | yes |  |  |

### `institution.approval_outbox`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | bigint | no | `nextval` | PK |
| `institution_id` | uuid | no |  |  |
| `event` | text | no |  |  |
| `payload` | jsonb | no |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `processed_at` | timestamptz | yes |  |  |

### `institution.approval_stage_def`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `template_id` | uuid | no |  | FK institution.approval_template |
| `seq` | integer | no |  |  |
| `name` | text | no |  |  |
| `stage_type` | text | no |  | role / committee |
| `committee_id` | uuid | yes |  | FK institution.approval_committee |
| `approver_role` | text | yes |  |  |
| `checklist` | jsonb | yes |  |  |
| `sla_hours` | integer | yes |  |  |
| `on_sla_breach` | text | no | `'notify'` | notify / escalate |
| `escalate_to_template_id` | uuid | yes |  | FK institution.approval_template |
| `config` | jsonb | no | `'{}'` |  |

### `institution.approval_stage_instance`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `instance_id` | uuid | no |  | FK institution.approval_instance |
| `seq` | integer | no |  |  |
| `stage_def_id` | uuid | no |  | FK institution.approval_stage_def |
| `status` | text | no | `'pending'` |  |
| `started_at` | timestamptz | yes |  |  |
| `due_at` | timestamptz | yes |  |  |
| `resolved_at` | timestamptz | yes |  |  |

### `institution.approval_template`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  |  |
| `name` | text | no |  |  |
| `entity_type` | text | no |  |  |
| `version` | integer | no | `1` |  |
| `status` | text | no | `'draft'` | draft / active |
| `created_by` | uuid | no |  |  |
| `approved_by` | uuid | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.benefit`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `product_id` | uuid | yes |  | FK catalog.product |
| `cat_id` | uuid | no |  | FK institution.benefit_cat |
| `title` | text | no |  |  |
| `description` | text | yes |  |  |
| `value_display` | text | yes |  |  |
| `is_guaranteed` | boolean | no | `false` |  |
| `conditions` | text | yes |  |  |
| `valid_from` | date | yes |  |  |
| `valid_until` | date | yes |  |  |
| `is_active` | boolean | no | `true` |  |
| `created_by` | uuid | yes |  | FK institution.member |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.benefit_cat`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `code` | text | no |  |  |
| `label` | text | no |  |  |
| `icon_key` | text | yes |  |  |
| `sort_order` | integer | no | `0` |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.committee_member`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `committee_id` | uuid | no |  | FK institution.approval_committee |
| `member_id` | uuid | no |  |  |
| `role` | text | no | `'member'` | chair / member |
| `is_voting` | boolean | no | `true` |  |
| `valid_from` | date | no | `CURRENT_DATE` |  |
| `valid_to` | date | yes |  |  |
| `created_by` | uuid | no |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.doa_rule`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  |  |
| `entity_type` | text | no |  |  |
| `priority` | integer | no |  | lowest wins |
| `conditions` | jsonb | no | `'{}'` |  |
| `template_id` | uuid | no |  | FK institution.approval_template |
| `status` | text | no | `'active'` |  |
| `created_by` | uuid | no |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.doc`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `doc_type_id` | uuid | no |  | FK institution.doc_type |
| `storage_path` | text | no |  |  |
| `file_name` | text | no |  |  |
| `mime_type` | text | yes |  |  |
| `status` | text | no | `'pending'` |  |
| `expiry_date` | date | yes |  |  |
| `rejection_reason` | text | yes |  |  |
| `reviewed_by` | uuid | yes |  | FK portal_admin.admin_users |
| `reviewed_at` | timestamptz | yes |  |  |
| `uploaded_by` | uuid | yes |  | FK institution.member |
| `uploaded_at` | timestamptz | no | `now()` |  |

### `institution.doc_generation`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `template_id` | uuid | no |  | FK institution.doc_template |
| `template_version_id` | uuid | no |  | FK institution.doc_template_version |
| `entity_type` | text | no | `'loan_pipeline'` |  |
| `entity_id` | uuid | no |  |  |
| `stage_instance_id` | uuid | yes |  | FK marketplace.pipeline_stage_instance |
| `data_snapshot` | jsonb | no | `'{}'` | merge data frozen at generation |
| `output_docx_path` | text | yes |  |  |
| `output_pdf_path` | text | yes |  |  |
| `status` | text | no | `'pending'` |  |
| `error` | text | yes |  |  |
| `esign_envelope_id` | uuid | yes |  |  |
| `generated_by` | uuid | yes |  | FK institution.member |
| `generated_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.doc_template`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `product_id` | uuid | yes |  | FK catalog.product |
| `product_code` | text | yes |  |  |
| `code` | text | no |  |  |
| `name` | text | no |  |  |
| `description` | text | yes |  |  |
| `doc_category` | text | no | `'other'` |  |
| `status` | text | no | `'draft'` |  |
| `current_version` | integer | no | `0` |  |
| `created_by` | uuid | yes |  | FK institution.member |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.doc_template_version`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `template_id` | uuid | no |  | FK institution.doc_template |
| `institution_id` | uuid | no |  | FK institution.institution |
| `version_no` | integer | no |  |  |
| `storage_path` | text | no |  |  |
| `file_name` | text | no |  |  |
| `mime_type` | text | no | `docx` |  |
| `file_size_bytes` | integer | yes |  |  |
| `checksum_sha256` | text | yes |  |  |
| `merge_field_map` | jsonb | no | `'{}'` |  |
| `change_note` | text | yes |  |  |
| `status` | text | no | `'draft'` | draft/pending/active/rejected |
| `created_by` | uuid | yes |  | FK institution.member |
| `approved_by` | uuid | yes |  | FK institution.member |
| `approved_at` | timestamptz | yes |  |  |
| `rejection_note` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.doc_type`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `code` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | yes |  |  |
| `is_mandatory` | boolean | no | `true` |  |
| `applies_to` | text[] | yes |  |  |
| `sort_order` | integer | no | `0` |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.group`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `slug` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no | `''` |  |
| `module_permissions` | text[] | no | `'{}'` | inst:* module keys |
| `product_scope` | uuid[] | yes |  | null = all products |
| `is_system` | boolean | no | `false` |  |
| `created_by` | uuid | yes |  | FK institution.member |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.institution`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `name` | varchar | no |  |  |
| `legal_name` | varchar | no |  |  |
| `institution_type` | varchar | no |  |  |
| `reg_number` | varchar | yes |  |  |
| `regulator` | varchar | yes |  |  |
| `country` | varchar | no |  |  |
| `website` | varchar | yes |  |  |
| `deployment_model` | varchar | no | `'saas'` | saas/paas/on_prem |
| `modules` | jsonb | no | `'{}'` | licensed module entitlements |
| `onboarding_stage` | varchar | no | `'registered'` |  |
| `compliance_status` | varchar | no | `'not_submitted'` |  |
| `compliance_notes` | text | yes |  |  |
| `compliance_reviewed_at` | timestamptz | yes |  |  |
| `compliance_reviewed_by` | uuid | yes |  |  |
| `approved` | boolean | no | `false` |  |
| `approved_at` | timestamptz | yes |  |  |
| `approved_by` | uuid | yes |  |  |
| `suspended_at` | timestamptz | yes |  |  |
| `suspended_by` | uuid | yes |  |  |
| `suspension_reason` | text | yes |  |  |
| `offboarded_at` | timestamptz | yes |  |  |
| `primary_contact_name` | varchar | yes |  |  |
| `primary_contact_email` | varchar | yes |  |  |
| `primary_contact_phone` | varchar | yes |  |  |
| `tax_id` | text | yes |  |  |
| `incorporation_date` | date | yes |  |  |
| `logo_url` | text | yes |  |  |
| `timezone` | text | no | `'Indian/Mauritius'` |  |
| `notes` | text | yes |  |  |
| `metadata` | jsonb | no | `'{}'` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.institution_sla_config`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `product_id` | uuid | yes |  | FK catalog.product |
| `product_code` | text | yes |  |  |
| `bid_window_minutes` | integer | yes |  | overrides catalog.product_sla |
| `auto_withdraw_minutes` | integer | yes |  |  |
| `integration_mode` | text | yes |  | portal/webhook/api_pull/core_banking |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.kyb_document`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `doc_type` | text | no |  |  |
| `label` | text | no |  |  |
| `storage_path` | text | no |  |  |
| `mime_type` | text | no |  |  |
| `file_size_bytes` | integer | yes |  |  |
| `status` | text | no | `'pending'` |  |
| `rejection_reason` | text | yes |  |  |
| `reviewed_by` | uuid | yes |  |  |
| `reviewed_at` | timestamptz | yes |  |  |
| `expires_at` | timestamptz | yes |  |  |
| `uploaded_by` | uuid | yes |  | FK institution.member |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.member`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `auth_user_id` | uuid | no |  | ficium-auth subject |
| `email` | varchar | no |  |  |
| `full_name` | varchar | yes |  |  |
| `role` | varchar | no | `'analyst'` |  |
| `member_role` | text | yes |  |  |
| `is_primary_admin` | boolean | no | `false` |  |
| `active` | boolean | no | `true` |  |
| `group_id` | uuid | yes |  | FK portal_admin.user_groups |
| `custom_group_id` | uuid | yes |  | FK institution.group |
| `system_group_id` | uuid | yes |  |  |
| `invited_by` | uuid | yes |  |  |
| `invited_at` | timestamptz | yes |  |  |
| `activated_at` | timestamptz | yes |  |  |
| `deactivated_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.pending_actions`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `action_category` | text | no |  | bid.* or internal |
| `action_status` | text | no | `'pending'` |  |
| `maker_id` | uuid | no |  | FK institution.member |
| `maker_role` | text | no | `''` |  |
| `institution_id` | uuid | no |  | FK institution.institution |
| `resource_type` | text | no |  |  |
| `resource_id` | uuid | yes |  |  |
| `payload` | jsonb | no | `'{}'` |  |
| `payload_before` | jsonb | yes |  |  |
| `checker_id` | uuid | yes |  | FK institution.member |
| `checker_role` | text | yes |  |  |
| `checker_note` | text | yes |  |  |
| `checked_at` | timestamptz | yes |  |  |
| `initiated_at` | timestamptz | no | `now()` |  |
| `expires_at` | timestamptz | no | `now()+7d` |  |
| `execution_status` | text | yes | `'pending'` |  |
| `executed_at` | timestamptz | yes |  |  |
| `execution_error` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.pipeline_stage_def`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `template_id` | uuid | no |  | FK institution.pipeline_template |
| `institution_id` | uuid | no |  |  |
| `stage_key` | stage_key_enum | no | `'custom'` |  |
| `custom_key` | text | yes |  |  |
| `label` | text | no |  |  |
| `description` | text | yes |  |  |
| `position` | smallint | no |  |  |
| `sla_hours` | integer | no | `48` |  |
| `requires_maker_checker` | boolean | no | `false` |  |
| `requires_documents` | boolean | no | `false` |  |
| `borrower_label` | text | yes |  | shown to borrower |
| `borrower_visible` | boolean | no | `true` |  |
| `is_active` | boolean | no | `true` |  |
| `created_at` | timestamptz | no | `now()` |  |

### `institution.pipeline_template`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `name` | text | no |  |  |
| `description` | text | yes |  |  |
| `product_code` | text | yes |  |  |
| `is_default` | boolean | no | `false` |  |
| `is_active` | boolean | no | `true` |  |
| `created_by` | uuid | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.product_config`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `product_id` | uuid | no |  | FK catalog.product |
| `enabled` | boolean | no | `true` |  |
| `min_rate` | numeric | yes |  |  |
| `max_rate` | numeric | yes |  |  |
| `min_amount` | numeric | yes |  |  |
| `max_amount` | numeric | yes |  |  |
| `min_term_months` | integer | yes |  |  |
| `max_term_months` | integer | yes |  |  |
| `bid_window_minutes` | integer | yes |  |  |
| `auto_withdraw_minutes` | integer | yes |  |  |
| `conditions` | jsonb | no | `'{}'` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.webhook`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `institution_id` | uuid | no |  | FK institution.institution |
| `label` | text | no |  |  |
| `endpoint_url` | text | no |  | SSRF-validated |
| `secret_hash` | text | no |  |  |
| `signing_secret` | text | no | `''` | HMAC-SHA256 key |
| `event_types` | jsonb | no | `'[]'` |  |
| `active` | boolean | no | `true` |  |
| `retry_max` | integer | no | `3` |  |
| `timeout_ms` | integer | no | `30000` |  |
| `failure_count` | integer | no | `0` | circuit breaker |
| `last_fired_at` | timestamptz | yes |  |  |
| `last_status` | text | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `institution.webhook_delivery`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `webhook_id` | uuid | no |  | FK institution.webhook |
| `institution_id` | uuid | no |  | FK institution.institution |
| `event_type` | text | no |  |  |
| `event_id` | uuid | no |  |  |
| `payload` | jsonb | no | `'{}'` |  |
| `status` | text | no | `'pending'` |  |
| `attempts` | integer | no | `0` |  |
| `last_attempt_at` | timestamptz | yes |  |  |
| `response_status` | integer | yes |  |  |
| `response_body` | text | yes |  |  |
| `next_retry_at` | timestamptz | yes |  | exponential backoff |
| `delivered_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

## Schema `marketplace`

### `marketplace.acceptance`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK marketplace.request |
| `bid_id` | uuid | no |  | FK marketplace.bid |
| `accepted_by_consumer` | uuid | no |  |  |
| `acceptance_method` | text | no | `'app'` |  |
| `terms_version` | text | yes |  |  |
| `ip` | inet | yes |  |  |
| `metadata` | jsonb | no | `'{}'` |  |
| `accepted_at` | timestamptz | no | `now()` |  |

### `marketplace.bid`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK marketplace.request |
| `institution_id` | uuid | no |  | FK institution.institution |
| `submitted_by` | uuid | yes |  | FK institution.member |
| `rate` | numeric(8,4) | no |  |  |
| `rate_type` | text | no | `'fixed'` |  |
| `rate_valid_days` | integer | yes |  |  |
| `amount_offered` | numeric(20,6) | no |  |  |
| `term_months` | integer | no |  |  |
| `conditions` | jsonb | no | `'{}'` |  |
| `fee_structure` | jsonb | no | `'{}'` |  |
| `status` | text | no | `'submitted'` |  |
| `submitted_via` | text | no | `'portal'` | portal / api / autobid |
| `submitted_at` | timestamptz | no | `now()` |  |
| `expires_at` | timestamptz | yes |  |  |
| `withdrawn_at` | timestamptz | yes |  |  |
| `withdraw_reason` | text | yes |  |  |
| `rejected_at` | timestamptz | yes |  |  |
| `rejection_reason` | text | yes |  |  |
| `response_time_ms` | integer | yes |  |  |
| `idempotency_key` | text | yes |  |  |
| `metadata` | jsonb | no | `'{}'` |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `marketplace.bid_acceptance`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK marketplace.request |
| `bid_id` | uuid | no |  | FK marketplace.bid |
| `institution_id` | uuid | no |  |  |
| `full_name` | text | no |  | PHASE 2 identity reveal |
| `email` | text | no |  | PHASE 2 |
| `phone` | text | yes |  | PHASE 2 |
| `address` | text | yes |  | PHASE 2 |
| `date_of_birth` | date | yes |  | PHASE 2 |
| `document_number` | text | yes |  | PHASE 2 |
| `revealed_at` | timestamptz | no | `now()` |  |

### `marketplace.bid_allocation`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `bid_id` | uuid | no |  | FK marketplace.bid |
| `product_id` | uuid | no |  | FK catalog.product |
| `amount_offered` | numeric | no |  |  |
| `rate` | numeric | yes |  |  |
| `term_months` | integer | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |

### `marketplace.bid_benefit`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `bid_id` | uuid | no |  | FK marketplace.bid |
| `benefit_id` | uuid | no |  | FK institution.benefit |
| `title` | text | no |  | snapshot at bid time |
| `description` | text | yes |  |  |
| `value_display` | text | yes |  |  |
| `is_guaranteed` | boolean | no |  |  |
| `cat_code` | text | no |  |  |

### `marketplace.bid_event`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `bid_id` | uuid | no |  | FK marketplace.bid |
| `from_status` | text | yes |  |  |
| `to_status` | text | no |  |  |
| `actor_id` | uuid | yes |  |  |
| `actor_type` | text | no | `'system'` |  |
| `reason` | text | yes |  |  |
| `metadata` | jsonb | no | `'{}'` |  |
| `occurred_at` | timestamptz | no | `now()` | append-only |

### `marketplace.loan_pipeline`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK marketplace.request |
| `bid_id` | uuid | no |  | FK marketplace.bid |
| `institution_id` | uuid | no |  |  |
| `consumer_id` | uuid | no |  |  |
| `template_id` | uuid | no |  | FK institution.pipeline_template |
| `current_stage_id` | uuid | yes |  | FK marketplace.pipeline_stage_instance |
| `status` | pipeline_status_enum | no | `'active'` |  |
| `deal_amount` | numeric(15,2) | no |  |  |
| `deal_rate` | numeric(6,4) | no |  |  |
| `deal_term_months` | integer | no |  |  |
| `started_at` | timestamptz | no | `now()` |  |
| `completed_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `marketplace.pipeline_stage_instance`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `pipeline_id` | uuid | no |  | FK marketplace.loan_pipeline |
| `stage_def_id` | uuid | no |  | FK institution.pipeline_stage_def |
| `position` | smallint | no |  |  |
| `status` | stage_status_enum | no | `'pending'` |  |
| `submitted_by` | uuid | yes |  | maker |
| `submitted_at` | timestamptz | yes |  |  |
| `approved_by` | uuid | yes |  | checker |
| `approved_at` | timestamptz | yes |  |  |
| `rejection_reason` | text | yes |  |  |
| `notes` | text | yes |  |  |
| `documents` | jsonb | yes | `'[]'` |  |
| `started_at` | timestamptz | yes |  |  |
| `sla_due_at` | timestamptz | yes |  |  |
| `completed_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `marketplace.request`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `consumer_id` | uuid | no |  | opaque; App DB client id |
| `consumer_ref` | text | yes |  | anonymised display ref |
| `product_id` | uuid | no |  | FK catalog.product |
| `country` | char(2) | no | `'MU'` | FK catalog.country |
| `currency` | char(3) | no | `'MUR'` | FK catalog.currency |
| `amount` | numeric(20,6) | no |  |  |
| `term_months` | integer | no |  |  |
| `params` | jsonb | no | `'{}'` |  |
| `metadata` | jsonb | no | `'{}'` | phase1 payload incl. investment_profile |
| `status` | text | no | `'open'` |  |
| `bid_window_opens_at` | timestamptz | no | `now()` |  |
| `bid_window_closes_at` | timestamptz | no | `now()+4h` |  |
| `auto_close_at` | timestamptz | yes |  |  |
| `winning_bid_id` | uuid | yes |  | FK marketplace.bid |
| `accepted_at` | timestamptz | yes |  |  |
| `cancelled_at` | timestamptz | yes |  |  |
| `cancellation_reason` | text | yes |  |  |
| `idempotency_key` | text | no |  | dedupes App DB ingest |
| `source` | text | no | `'app'` |  |
| `relist_count` | smallint | no | `0` |  |
| `relisted_from` | uuid | yes |  | FK marketplace.request |
| `notified_at` | timestamptz | yes |  |  |
| `ficium_risk_tier` | text | yes |  | from rating engine |
| `ficium_score` | numeric(6,2) | yes |  | from rating engine |
| `allocation_mode` | text | yes |  | single / basket |
| `created_at` | timestamptz | no | `now()` |  |
| `updated_at` | timestamptz | no | `now()` |  |

### `marketplace.request_allocation`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` | PK |
| `request_id` | uuid | no |  | FK marketplace.request |
| `product_id` | uuid | no |  | FK catalog.product |
| `amount` | numeric | yes |  |  |
| `sort_order` | integer | no | `0` |  |
| `created_at` | timestamptz | no | `now()` |  |

### `marketplace.sync_state`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | smallint | no | `1` | PK singleton row |
| `last_updated_at` | timestamptz | no | `epoch` | keyset cursor part 1 |
| `last_id` | uuid | no | `uuid-zero` | keyset cursor part 2 (tie-break) |
| `last_run_at` | timestamptz | yes |  |  |
| `last_run_pulled` | integer | no | `0` |  |
| `last_run_synced` | integer | no | `0` |  |
| `last_run_failed` | integer | no | `0` |  |

## Schema `admin`

### `admin.commission_event`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `pipeline_id` | uuid | no |  |  |
| `request_id` | uuid | no |  |  |
| `institution_id` | uuid | no |  |  |
| `deal_amount` | numeric | no |  |  |
| `commission_rate` | numeric | no |  |  |
| `commission_amt` | numeric | yes |  |  |
| `currency` | char(3) | no |  |  |
| `status` | text | no |  |  |
| `invoice_ref` | text | yes |  |  |
| `invoiced_at` | timestamptz | yes |  |  |
| `paid_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `admin.notification_log`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `event_type` | text | no |  |  |
| `recipient_type` | text | no |  |  |
| `recipient_ref` | text | no |  |  |
| `related_id` | uuid | yes |  |  |
| `status` | text | no |  |  |
| `error_detail` | text | yes |  |  |
| `sent_at` | timestamptz | no |  |  |

### `admin.role`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `slug` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no |  |  |
| `permissions` | text[] | no |  |  |
| `is_system` | boolean | no |  |  |
| `created_at` | timestamptz | no |  |  |

### `admin.session`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `ip_address` | inet | no |  |  |
| `user_agent` | text | no |  |  |
| `country` | text | yes |  |  |
| `city` | text | yes |  |  |
| `started_at` | timestamptz | no |  |  |
| `last_active_at` | timestamptz | no |  |  |
| `ended_at` | timestamptz | yes |  |  |
| `end_reason` | text | yes |  |  |
| `is_active` | boolean | no |  |  |

### `admin.system_group`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `slug` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no |  |  |
| `side` | text | no |  | institution / admin |
| `module_permissions` | text[] | no |  |  |
| `is_system` | boolean | no |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `admin.user`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `auth_user_id` | uuid | no |  |  |
| `email` | text | no |  |  |
| `display_name` | text | no |  |  |
| `role_id` | uuid | yes |  | FK admin.role |
| `role_slug` | text | no |  |  |
| `system_group_id` | uuid | yes |  |  |
| `status` | text | no |  |  |
| `mfa_enabled` | boolean | no |  |  |
| `mfa_verified_at` | timestamptz | yes |  |  |
| `failed_login_count` | integer | no |  |  |
| `locked_at` | timestamptz | yes |  |  |
| `locked_reason` | text | yes |  |  |
| `force_password_reset` | boolean | no |  |  |
| `last_login_at` | timestamptz | yes |  |  |
| `last_login_ip` | inet | yes |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |


## Schema `audit`

### `audit.event`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `occurred_at` | timestamptz | no |  |  |
| `actor_id` | uuid | yes |  |  |
| `actor_type` | text | no |  |  |
| `actor_email` | text | yes |  |  |
| `actor_role` | text | yes |  |  |
| `actor_ip` | inet | yes |  |  |
| `actor_user_agent` | text | yes |  |  |
| `institution_id` | uuid | yes |  |  |
| `action` | text | no |  |  |
| `resource_type` | text | yes |  |  |
| `resource_id` | uuid | yes |  |  |
| `resource_label` | text | yes |  |  |
| `outcome` | text | no |  |  |
| `outcome_note` | text | yes |  |  |
| `governance_action_id` | uuid | yes |  |  |
| `session_id` | uuid | yes |  |  |
| `request_id` | text | yes |  | correlation id |
| `metadata` | jsonb | no |  |  |


## Schema `auth_portal`

### `auth_portal.auth_users`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `institution_id` | uuid | no |  |  |
| `email` | varchar | no |  |  |
| `username` | text | yes |  | portal login handle |
| `email_verified` | boolean | no |  |  |
| `password_hash` | text | no |  |  |
| `role` | varchar | no |  |  |
| `is_active` | boolean | no |  |  |
| `mfa_enabled` | boolean | no |  |  |
| `mfa_secret` | text | yes |  |  |
| `must_change_password` | boolean | no |  |  |
| `password_changed_at` | timestamptz | no |  |  |
| `failed_attempts` | integer | no |  |  |
| `locked_until` | timestamptz | yes |  |  |
| `last_login_at` | timestamptz | yes |  |  |
| `last_login_ip` | varchar | yes |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `auth_portal.auth_sessions`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  | FK auth_portal.auth_users |
| `refresh_token_hash` | varchar | no |  |  |
| `ip_address` | varchar | yes |  |  |
| `user_agent` | text | yes |  |  |
| `device_fingerprint` | varchar | yes |  |  |
| `is_active` | boolean | no |  |  |
| `created_at` | timestamptz | no |  |  |
| `last_used_at` | timestamptz | no |  |  |
| `expires_at` | timestamptz | no |  |  |
| `revoked_at` | timestamptz | yes |  |  |
| `revoke_reason` | varchar | yes |  |  |

### `auth_portal.auth_audit_events`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | yes |  |  |
| `institution_id` | uuid | yes |  |  |
| `event_type` | varchar | no |  |  |
| `outcome` | varchar | no |  |  |
| `ip_address` | varchar | yes |  |  |
| `user_agent` | text | yes |  |  |
| `event_metadata` | jsonb | yes |  |  |
| `created_at` | timestamptz | no |  |  |

### `auth_portal.email_verification_tokens`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `token_hash` | varchar | no |  |  |
| `used` | boolean | no |  |  |
| `created_at` | timestamptz | no |  |  |
| `expires_at` | timestamptz | no |  |  |

### `auth_portal.password_reset_tokens`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `token_hash` | varchar | no |  |  |
| `used` | boolean | no |  |  |
| `used_at` | timestamptz | yes |  |  |
| `ip_address` | varchar | yes |  |  |
| `created_at` | timestamptz | no |  |  |
| `expires_at` | timestamptz | no |  |  |

### `auth_portal.mfa_backup_codes`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `code_hash` | varchar | no |  |  |
| `used_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no |  |  |

### `auth_portal.ip_allowlist`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `institution_id` | uuid | no |  |  |
| `cidr` | varchar | no |  |  |
| `label` | varchar | yes |  |  |
| `created_by` | uuid | no |  |  |
| `created_at` | timestamptz | no |  |  |


## Schema `governance`

### `governance.action`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `scope` | text | no |  | institution / platform |
| `category` | text | no |  |  |
| `label` | text | no |  |  |
| `risk` | text | no |  | low/medium/high/critical |
| `institution_id` | uuid | yes |  |  |
| `maker_id` | uuid | no |  |  |
| `maker_role` | text | no |  |  |
| `maker_ip` | inet | yes |  |  |
| `maker_user_agent` | text | yes |  |  |
| `resource_type` | text | no |  |  |
| `resource_id` | uuid | yes |  |  |
| `resource_label` | text | yes |  |  |
| `payload` | jsonb | no |  |  |
| `payload_before` | jsonb | yes |  |  |
| `status` | text | no |  |  |
| `checker_id` | uuid | yes |  | must differ from maker |
| `checker_role` | text | yes |  |  |
| `checker_note` | text | yes |  |  |
| `checker_ip` | inet | yes |  |  |
| `checked_at` | timestamptz | yes |  |  |
| `execution_status` | text | no |  |  |
| `executed_at` | timestamptz | yes |  |  |
| `execution_error` | text | yes |  |  |
| `expires_at` | timestamptz | no |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |


## Schema `identity`

### `identity.profile`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `email` | text | no |  |  |
| `display_name` | text | no |  |  |
| `phone` | text | yes |  |  |
| `avatar_url` | text | yes |  |  |
| `preferred_locale` | text | no |  |  |
| `status` | text | no |  |  |
| `mfa_totp_enabled` | boolean | no |  |  |
| `mfa_totp_verified_at` | timestamptz | yes |  |  |
| `failed_login_count` | integer | no |  |  |
| `locked_at` | timestamptz | yes |  |  |
| `locked_reason` | text | yes |  |  |
| `force_password_reset` | boolean | no |  |  |
| `last_login_at` | timestamptz | yes |  |  |
| `last_login_ip` | inet | yes |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `identity.login_event`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | yes |  |  |
| `email` | text | yes |  |  |
| `ip` | inet | yes |  |  |
| `user_agent` | text | yes |  |  |
| `country` | text | yes |  |  |
| `city` | text | yes |  |  |
| `outcome` | text | no |  |  |
| `failure_reason` | text | yes |  |  |
| `occurred_at` | timestamptz | no |  |  |

### `identity.email_verification_token`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `email` | text | no |  |  |
| `token_hash` | text | no |  |  |
| `expires_at` | timestamptz | no |  |  |
| `verified_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no |  |  |

### `identity.password_reset_token`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `token_hash` | text | no |  |  |
| `expires_at` | timestamptz | no |  |  |
| `used_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no |  |  |

### `identity.mfa_backup_code`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `code_hash` | text | no |  |  |
| `used_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no |  |  |

### `identity.ip_allowlist`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `user_id` | uuid | no |  |  |
| `cidr` | cidr | no |  |  |
| `label` | text | no |  |  |
| `created_by` | uuid | yes |  |  |
| `created_at` | timestamptz | no |  |  |


## Schema `portal_admin`

### `portal_admin.admin_users`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `auth_user_id` | uuid | no |  |  |
| `email` | text | no |  |  |
| `display_name` | text | no |  |  |
| `role_slug` | text | no |  |  |
| `custom_role_id` | uuid | yes |  | FK portal_admin.admin_roles |
| `group_id` | uuid | yes |  | FK portal_admin.user_groups |
| `status` | admin_user_status | no |  |  |
| `mfa_enabled` | boolean | no |  |  |
| `mfa_verified_at` | timestamptz | yes |  |  |
| `last_login_at` | timestamptz | yes |  |  |
| `last_login_ip` | inet | yes |  |  |
| `failed_login_count` | integer | no |  |  |
| `locked_at` | timestamptz | yes |  |  |
| `locked_reason` | text | yes |  |  |
| `suspended_at` | timestamptz | yes |  |  |
| `suspended_by` | uuid | yes |  |  |
| `suspension_reason` | text | yes |  |  |
| `password_changed_at` | timestamptz | yes |  |  |
| `force_password_reset` | boolean | no |  |  |
| `created_by` | uuid | no |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `portal_admin.admin_roles`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `slug` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no |  |  |
| `permissions` | text[] | no |  |  |
| `is_system` | boolean | no |  |  |
| `created_by` | uuid | no |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `portal_admin.user_groups`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `slug` | text | no |  |  |
| `label` | text | no |  |  |
| `description` | text | no |  |  |
| `user_type` | text | no |  | institution / admin |
| `module_permissions` | text[] | no |  |  |
| `is_system` | boolean | no |  |  |
| `created_by` | uuid | no |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `portal_admin.admin_sessions`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `admin_user_id` | uuid | no |  |  |
| `ip_address` | inet | no |  |  |
| `user_agent` | text | no |  |  |
| `country` | text | yes |  |  |
| `city` | text | yes |  |  |
| `started_at` | timestamptz | no |  |  |
| `last_active_at` | timestamptz | no |  |  |
| `ended_at` | timestamptz | yes |  |  |
| `end_reason` | text | yes |  |  |
| `is_active` | boolean | no |  |  |

### `portal_admin.admin_dual_control_actions`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `action_category` | text | no |  |  |
| `action_label` | text | no |  |  |
| `risk` | action_risk | no |  |  |
| `maker_id` | uuid | no |  |  |
| `maker_email` | text | no |  |  |
| `maker_role` | text | no |  |  |
| `maker_ip` | inet | no |  |  |
| `resource_type` | text | no |  |  |
| `resource_id` | uuid | yes |  |  |
| `resource_label` | text | yes |  |  |
| `payload` | jsonb | no |  |  |
| `payload_before` | jsonb | yes |  |  |
| `status` | dual_control_status | no |  |  |
| `checker_id` | uuid | yes |  |  |
| `checker_email` | text | yes |  |  |
| `checker_role` | text | yes |  |  |
| `checker_note` | text | yes |  |  |
| `checker_ip` | inet | yes |  |  |
| `checked_at` | timestamptz | yes |  |  |
| `initiated_at` | timestamptz | no |  |  |
| `expires_at` | timestamptz | no |  |  |
| `executed_at` | timestamptz | yes |  |  |
| `execution_error` | text | yes |  |  |

### `portal_admin.admin_audit_log`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `session_id` | uuid | yes |  |  |
| `actor_id` | uuid | yes |  |  |
| `actor_email` | text | yes |  |  |
| `actor_role` | text | yes |  |  |
| `actor_ip` | inet | yes |  |  |
| `action_category` | text | no |  |  |
| `event_label` | text | no |  |  |
| `resource_type` | text | yes |  |  |
| `resource_id` | uuid | yes |  |  |
| `resource_label` | text | yes |  |  |
| `dual_control_id` | uuid | yes |  |  |
| `state_before` | jsonb | yes |  |  |
| `state_after` | jsonb | yes |  |  |
| `outcome` | audit_outcome | no |  |  |
| `outcome_note` | text | yes |  |  |
| `created_at` | timestamptz | no |  |  |


## Schema `public`

### `public.portal_notifications`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `institution_id` | uuid | no |  |  |
| `kind` | text | no |  |  |
| `title` | text | no |  |  |
| `body` | text | yes |  |  |
| `link` | text | yes |  |  |
| `metadata` | jsonb | yes |  |  |
| `read_at` | timestamptz | yes |  |  |
| `created_at` | timestamptz | no |  |  |

### `public._identity_migration_log`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | bigint | no |  | PK — one-off migration artefact |
| `run_at` | timestamptz | yes |  |  |
| `schema_name` | text | no |  |  |
| `table_name` | text | no |  |  |
| `column_name` | text | no |  |  |
| `old_uuid` | uuid | no |  |  |
| `new_uuid` | uuid | no |  |  |
| `email` | text | no |  |  |
| `dry_run` | boolean | no |  |  |


## Schema `workflow`

### `workflow.template`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `institution_id` | uuid | no |  |  |
| `product_type` | text | no |  |  |
| `name` | text | no |  |  |
| `description` | text | yes |  |  |
| `is_active` | boolean | no |  |  |
| `created_by` | uuid | yes |  |  |
| `created_at` | timestamptz | no |  |  |
| `updated_at` | timestamptz | no |  |  |

### `workflow.stage`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `template_id` | uuid | no |  | FK workflow.template |
| `position` | integer | no |  |  |
| `name` | text | no |  |  |
| `stage_type` | text | no |  |  |
| `description` | text | yes |  |  |
| `is_required` | boolean | no |  |  |
| `parallel_ok` | boolean | no |  |  |
| `sla_hours` | integer | yes |  |  |
| `escalate_after_hours` | integer | yes |  |  |
| `created_at` | timestamptz | no |  |  |

### `workflow.stage_assignment`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `stage_id` | uuid | no |  | FK workflow.stage |
| `department` | text | yes |  |  |
| `assign_mode` | text | no |  |  |
| `default_assignee_id` | uuid | yes |  |  |
| `requires_dual_sign` | boolean | no |  |  |
| `created_at` | timestamptz | no |  |  |

### `workflow.doc_requirement`

| Column | Type | Null | Default | Key / Notes |
|---|---|---|---|---|
| `id` | uuid | no |  | PK |
| `stage_id` | uuid | no |  | FK workflow.stage |
| `doc_key` | text | no |  |  |
| `label` | text | no |  |  |
| `is_mandatory` | boolean | no |  |  |
| `position` | integer | no |  |  |
| `created_at` | timestamptz | no |  |  |
