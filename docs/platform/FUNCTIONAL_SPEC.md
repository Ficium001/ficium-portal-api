# Ficium — Functional Specification

_Last verified 1 August 2026 against the shipped code in all three repositories._

This document describes what the platform does, for whom, and under what rules.
For how it is built, see `ARCHITECTURE.md`. For the schema, see
`DATA_DICTIONARY.md`.

## 1. Actors

| Actor | Surface | Authentication |
|---|---|---|
| **Individual borrower** | `ficium` | Supabase Auth (email) |
| **Business borrower** | `ficium` | Supabase Auth (email) |
| **Bank officer** | `ficium-portal` | `ficium-auth` (username) |
| **Bank checker / approver** | `ficium-portal` | `ficium-auth` |
| **Institution admin** | `ficium-portal` | `ficium-auth` |
| **Ficium platform admin** | `ficium-portal` (`/admin/*`) | `ficium-auth` |
| **Machine consumer** | `/v1/*` REST | API key `fic_live_<64 hex>` |

## 2. Products

Seventeen product types, split into two families that drive genuinely different
UI, intake questions and bid presentation.

**Credit** — `personal_loan`, `mortgage`, `sme_loan`, `business_loan`,
`credit_card`, `leasing`, `overdraft`.

**Deposit / investment** — `fixed_deposit`, `savings_account`,
`savings_plan`, `business_account`, `investment_account`, `equities`,
`unit_trust`, `government_bonds`, `offshore_investment`, `mixed_portfolio`.

The split matters because credit-only sections — DSR, collateral, existing loans,
credit score, affordability, employment — are suppressed for
deposit/investment products, and an investment profile section is shown instead.
The split is currently encoded twice, as `_CREDIT_PRODUCT_TYPES` in
`ficium-portal-api` and `CREDIT_PRODUCT_CODES` in `ficium-portal`. **These two
lists must be changed together.** A product added to one and not the other
renders the wrong sections on the bid screen.

A single request may be a **basket** — multiple products with per-product
allocations (`request_allocations`), with institutions bidding per allocation
(`bid_allocation`).

## 3. Borrower journey

### 3.1 Registration and KYC

1. **Register** as individual or business. Optional **Scan NIC** at signup:
   Claude Vision extracts first name, last name, sex and date of birth from the
   ID and prefills the form. This runs pre-authentication and is IP-rate-limited
   via `kyc_scan_attempts`.
2. **Email verification.**
3. **KYC** (`/onboarding/kyc`) — ID document, selfie, proof of address. Scanning
   the NIC again here prefills document number and date of birth via Rekognition
   OCR/MRZ. Checks are individually toggleable in `kyc_settings`: AI analysis,
   face match, duplicate face, OCR name match, proof of address, velocity,
   document reuse, liveness, MRZ validation, permit check.
4. **Dossier** (`/onboarding/dossier`) — employment, income, assets, liabilities,
   compliance declarations (PEP, source of wealth, tax residency).
5. Admin review where flagged; `kyc_status` moves
   `pending → under_review → verified | rejected`.

A borrower **cannot post a request until KYC is verified.**

### 3.2 Creating a request

`/requests/new`. Product selection, then a product-specific question flow
(`PRODUCT_QUESTIONS` in `NewRequest.tsx`), then amount, term, maximum acceptable
rate and decision deadline.

Answers persist two ways, deliberately:

- `requests.purpose` — a human-readable pipe-delimited summary.
- `requests.product_answers` (jsonb) — the structured key/value answers,
  excluding the internal `__amount` / `__term` keys.

For investment products the structured answers carry risk appetite, investment
horizon, liquidity needs, investment style, target amount and monthly
contribution. `_build_investment_profile()` in `ficium-portal-api` shapes these
for the bid screen.

**Numeric coercion is required.** The question flow stores *every* answer as a
string, including `type:"number"` fields. `target_amount` and
`monthly_contribution` are typed `number | null` on the portal side, so the API
coerces them explicitly. Rendering used to work only by JavaScript coercion
accident.

### 3.3 Multi-participant requests

A request can include co-applicants and guarantors. The initiator invites by
email or SMS; the invitation carries a proposed role, liability type
(`joint_and_several | several | guarantor`) and ownership in basis points, and
expires after 7 days. The request sits in `awaiting_consent` until every invited
participant consents — `can_release_request()` is the gate.

### 3.4 Bidding and acceptance

The request is anonymised and published to eligible institutions. During the bid
window the borrower sees bids arrive in real time — rate, amount, term,
conditions, fees, and any benefits the institution attached.

**Structured chat** is available per lender while bidding is open. The borrower
picks from 8 template questions; the lender answers from 11 templates, 6 of which
take typed parameters. No free text on either side pre-acceptance.

Accepting a bid:

- freezes all other bids as rejected and locks their chat threads;
- reveals the borrower's identity to the winning institution only —
  name, email, phone, address, date of birth, document number;
- opens the loan pipeline;
- unlocks free-text chat with the winner.

### 3.5 Post-acceptance

The borrower tracks pipeline progress through stages the institution has marked
`borrower_visible`, under the `borrower_label` rather than the internal stage
name. Documents requiring signature arrive as an e-signature ceremony with OTP
verification.

### 3.6 Standing borrower features

| Feature | Route | Summary |
|---|---|---|
| Dashboard | `/dashboard` | Net worth, requests, credit card tile, activity |
| Net worth | `/networth` | Assets, liabilities, historical trend |
| Finances | `/finances` | Bank accounts and investment holdings, live pricing via Finnhub/CoinGecko, feeds net worth |
| Vault | `/vault` | Document store with AI extraction, property records, access log, retention dates |
| Markets | `/markets` | Rates, FX, deposit comparison, news in "everyday" or "finance" register |
| FICO advisor | `/advisor` | AI relationship manager (see §7) |
| Financial health | `/health` | Composite health / risk / affordability score |
| Couple | `/couple` | Joint finances with a verified partner (see §3.7) |
| Tools | `/tools` | ROI and other calculators |
| Alerts | `/alerts` | Notifications with unread count and clear-all |
| Activity | `/activity` | Personal audit trail |

### 3.7 Couple finance

Two verified borrowers can link into a `couple_link`. Verification is by marriage
certificate: the document is uploaded to the vault, Claude Vision OCRs it, and
both partners' names are matched against the extracted text
(`both_matched | partial_match | no_match`). RLS policies are symmetric — either
partner can read the link, neither can read the other's underlying accounts
without it. Either partner may dissolve the link.

## 4. Institution journey

### 4.1 Onboarding

`registered → commercial_review → deployment_selected → modules_assigned →
technical_setup → compliance_review → pending_approval → approved`, with
`suspended` reachable from any state.

KYB documents are uploaded per `doc_type` and reviewed by a Ficium admin.
`institution.compliance` rolls this up into `can_bid` — an institution with
missing mandatory documents cannot bid regardless of its module entitlements.

### 4.2 Modules

Sixteen institution modules. Each is separately licensed
(`institution.institution.modules`) and separately permissioned per group
(`module_permissions[]`).

| Key | Label | Function |
|---|---|---|
| `inst:dashboard` | Dashboard | Tenant KPIs |
| `inst:marketplace` | Marketplace | Browse and filter anonymised requests |
| `inst:bids` | Bids | Submitted bids and outcomes |
| `inst:bid_approval` | Approval | Checker queue for bids |
| `inst:approvals` | Approval Chains | Committees, DoA, multi-stage chains |
| `inst:esign` | E-Signatures | Envelopes, ceremony, sealed audit trail |
| `inst:doctemplates` | Doc Templates | Design agreements, generate per deal |
| `inst:dual_control` | Dual Control | Four-eyes queue for internal actions |
| `inst:products` | Products | Product catalogue and rate configuration |
| `inst:benefits` | Benefits | Benefits attachable to bids |
| `inst:analytics` | Analytics | Win rate, deal value, bid performance |
| `inst:notifications` | Notifications | Bid, pipeline and approval alerts |
| `inst:pipeline` | Pipelines | Post-acceptance loan processing |
| `inst:audit` | Audit Trail | Read-only tenant audit |
| `inst:team` | User Management | Members and group assignment |
| `inst:settings` | Settings | Profile, compliance docs, API keys, webhooks |

Plus `inst:autobid` (rules engine) and `inst:documents`. **`inst:documents` has
no nav entry but is load-bearing** — it guards `GET /documents`, which backs the
Settings compliance tab and the e-sign document picker. Do not remove it as dead
code.

Eight admin modules: `admin:dashboard`, `users`, `groups`, `institutions`,
`dual_control`, `sessions`, `audit`, `system`.

### 4.3 Bidding

An officer opens a request and sees the Phase-1 view: product, amount, term,
purpose, anonymised brief, Ficium risk tier and score, and — for credit products
only — DSR, existing loans, employment and affordability. For
deposit/investment products those are replaced by the investment profile.

A bid carries rate, rate type, validity, amount offered, term, conditions, fee
structure, and optional benefits. Benefits are **snapshotted onto the bid**
(`bid_benefit`), so later edits to the benefit catalogue cannot rewrite a bid
that has already been made.

`marketplace.guard_bid_window()` rejects bids outside the open window.
Where `inst:bid_approval` is licensed, a bid enters the checker queue before
reaching the marketplace.

**Auto-bid** (`inst:autobid`) lets an institution define versioned rules that bid
automatically within set bounds. Rules follow a full lifecycle:
`create → new version → submit → approve/reject → pause/resume → retire`, with
approval required before a rule can act.

### 4.4 Pipeline

On acceptance a `loan_pipeline` is instantiated from the institution's
`pipeline_template`, one `pipeline_stage_instance` per `pipeline_stage_def`.
Standard stage keys: `credit_docs`, `offer_letter`, `legal_review`,
`board_approval`, `disbursement`, plus `custom`.

Each stage may require maker-checker, may require documents, has an SLA in hours,
and has independent borrower visibility and labelling. Stage status runs
`pending → active → awaiting_approval → completed`, with `skipped` and `blocked`
available.

### 4.5 Documents and signature

Templates are authored in Word with `{{ field }}` merge tags, uploaded as
versions, and published through maker-checker — the author cannot approve their
own version. Generating against a deal resolves a snapshot joining
`loan_pipeline`, `request` and the `bid_acceptance` identity reveal, merges via
docxtpl, and renders PDF through headless LibreOffice. Output lands in the
`institution-docs` Supabase Storage bucket.

Generated documents can be attached directly to an e-signature envelope, carrying
`doc_generation_id` and `approval_instance_id` so the chain from approval →
document → signature is traceable end to end.

### 4.6 Approval chains

A **committee** has members, voting rights, a chair, a quorum rule
(`quorum_type` / `quorum_value`) and a tie-break policy.

A **DoA rule** maps conditions — amount band, product, risk tier — to an approval
template, evaluated by priority with lowest-number-wins. `POST
/approval-engine/doa-rules/simulate` dry-runs routing without creating anything.

An **instance** freezes `template_version` and `entity_snapshot` at route time,
and records `entity_maker_id` so the maker is excluded from approving their own
work. Stages carry checklists and SLAs; on breach the stage either notifies or
escalates to another template.

**Delegation** transfers approval authority from one member to another for a
bounded period with a stated reason and its own approval. Actions taken under
delegation record both `actor_id` and `acting_as`.

## 5. Platform admin

Institution approval and suspension, KYB document review, user and group
management, session inspection and forced termination, platform-wide audit,
system configuration, and a dual-control queue for admin actions with a risk
classification (`low | medium | high | critical`).

`admin.commission_event` records per-deal commission — deal amount, rate,
computed amount, invoice reference and payment status. There is no UI for it yet.

## 6. Public API (`/v1/`)

For institutions integrating from their own systems rather than the portal.

Authentication is an API key of the form `fic_live_<64 hex characters>`, stored
as an HMAC. Key creation is maker-checker: `create → approve → active`, with
`revoke` and hard `delete`. Scopes are per key.

Endpoints: `GET /v1/requests`, `GET|POST /v1/bids`,
`PUT /v1/pipeline/{loan_id}/stages/{stage_id}/advance`,
`GET /v1/analytics/summary`, plus the autobid and entitlements routes.

**Webhooks** deliver events outbound, signed HMAC-SHA256, with exponential
backoff, a configurable retry ceiling and timeout, a failure counter acting as a
circuit breaker, and a full delivery log. Endpoint URLs are SSRF-validated on
registration.

## 7. FICO — AI advisor

A borrower-facing AI relationship manager built on Claude, feature-flagged
through `app_features`.

- Streaming chat over SSE.
- Rolling conversation summary persisted via Haiku, so long histories do not
  consume the whole context window.
- Atomic per-user message metering against a monthly quota
  (`fico.message_meter`, `consume_message()`).
- `max_tokens: 400`, temperature 0.8.

Regulatory guardrails in the system prompt: **inform, do not decide** — FICO does
not recommend a specific institution or product — and no Phase-1 identity
leakage.

## 8. Notifications

Borrower-side (`public.notifications`, kinds `kyc_verified`, `kyc_rejected`,
`request_created`, `request_expiring`, `bid_received`, `bid_accepted`,
`bid_expired`, `system`) with an unread badge driven by actual unread count and a
clear-all flow with optimistic update.

Institution-side (`public.portal_notifications`) covers new matching requests,
bid outcomes, pipeline stage changes, approval requests and SLA breaches.

Scheduled jobs on the App DB: `expire_overdue_requests()` hourly,
`notify_expiring_requests()`, `expire_stale_bids()`, `expire_pending_actions()`.

## 9. Audit and compliance

Every consequential action is recorded. Borrower actions in
`public.audit_events` via `write_client_audit()`; admin actions in
`admin.audit_events`; portal-side in `audit.event` via `audit.log()`, with a
`block_mutation` trigger enforcing append-only. Bid state transitions in
`marketplace.bid_event`, also append-only. Maker-checker in `governance.action`
and `institution.pending_actions`.

Audit rows capture actor identity, role, IP, user agent, resource,
before/after state, and outcome.

Regulatory position: FSC/BOM in Mauritius, with planned expansion to Africa and
India. Vendor SOC 2 / ISO 27001 certifications are relied on in place of a full
AWS migration.

## 10. Rules that are easy to break

Collected because each of these has caused a real defect.

1. **A new phase-1 field needs a Portal DB migration.**
   `marketplace.ingest_app_request()` drops anything not on its allowlist,
   silently, with a 200 response.
2. **`product_type` must match the frontend union.** Eight enum labels were
   missing for months while the frontend had full request flows built.
3. **The credit-product list exists twice** and both copies must move together.
4. **Chat placeholders are substituted server-side and unmatched ones are left
   verbatim.** A composer that does not collect params sends literal `{days}`.
5. **`sender_id` must never reach an institution.** It is the borrower's real
   `auth.uid()`, stable across every request they make.
6. **The API's service session bypasses RLS.** Constraints on that path belong in
   triggers or CHECKs.
7. **The bid PDF and the doc-template engine are unrelated.** Changing one does
   not change the other.
8. **`inst:documents` has no nav entry but is load-bearing.**
9. **The Vercel function ceiling is exactly met at 12.** A thirteenth root-level
   `api/*.ts` breaks the deploy.
10. **`tsc -b --force`, not `tsc --noEmit`,** when verifying locally.
