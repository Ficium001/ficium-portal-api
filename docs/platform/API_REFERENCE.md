# Ficium — API Reference

_Generated 1 August 2026 by scanning the route decorators in `ficium-portal-api`_
_and `ficium/api`. Regenerate with `build_api.py` rather than hand-editing._

## Authentication

| Surface | Scheme |
|---|---|
| Portal routes | `Authorization: Bearer <RS256 JWT>` from `ficium-auth`. `institution_id` and role are read from the claims. |
| `/v1/*` | `fic_live_<64 hex>` API key, verified against an HMAC. Scoped per key. |
| `/public/*` | Shared-secret server-to-server. Called by `ficium`'s Vercel functions, never from a browser. |
| `/esign/public/{token}` | Unauthenticated ceremony token plus OTP. |

Module-gated routes carry a `require_module(...)` dependency and return **403** when the institution lacks the entitlement — distinct from **401** for a bad token.

## Conventions

- All request and response bodies are JSON unless noted.
- Money is decimal; never parse it as a float.
- Timestamps are ISO 8601 with offset.
- Mutating endpoints accept an idempotency key where a retry could double-write.

## Marketplace

### `app/api/marketplace.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/marketplace/requests` | — | Anonymised (Phase 1) requests visible to the caller's institution |
| `GET` | `/marketplace/my-bids` | — | Bids submitted by the caller's institution, with request context |
| `POST` | `/marketplace/bids` | — | Place a bid. Rejected outside the open bid window |
| `GET` | `/marketplace/bids/{bid_id}` | — | Single bid detail |
| `GET` | `/marketplace/bids/{bid_id}/reveal` | — | Phase 2 borrower identity. Winning institution only, post-acceptance |
| `POST` | `/marketplace/requests/{request_id}/reject` | — | Decline to bid, with a reason |
| `POST` | `/marketplace/close-expired` | — | Sweep requests whose bid window has closed |
| `POST` | `/marketplace/sync-requests` | — | Pull the next batch from the App DB. `?full_resync=true` resets the cursor to epoch |
| `GET` | `/marketplace/sync-health` | — | Cursor position, last-run counts, `more_pending` |

### `app/api/request_chat.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/marketplace/requests/message-templates` | — | Structured chat catalogue, incl. `params_schema` |
| `GET` | `/marketplace/requests/{request_id}/messages` | — | Thread for one `(request_id, institution_id)` pair. `sender_id` masked |
| `POST` | `/marketplace/requests/{request_id}/messages` | — | Send a structured message; free text only post-acceptance for the winner |

## Bidding automation

### `app/api/autobid.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `POST` | `/v1/autobid/rules` | — | Create an auto-bid rule (starts in draft) |
| `POST` | `/v1/autobid/rules/{rule_id}/versions` | — | New immutable version of an existing rule |
| `POST` | `/v1/autobid/rules/{rule_id}/submit` | — | Submit a version for approval |
| `POST` | `/v1/autobid/rules/{rule_id}/approve` | — | Approve a version — required before it can act |
| `POST` | `/v1/autobid/rules/{rule_id}/reject` | — | Reject a submitted version |
| `POST` | `/v1/autobid/rules/{rule_id}/pause` | — | Suspend an active rule |
| `POST` | `/v1/autobid/rules/{rule_id}/resume` | — | Reactivate a paused rule |
| `POST` | `/v1/autobid/rules/{rule_id}/retire` | — | Permanently retire a rule |
| `GET` | `/v1/autobid/rules` | — | All rules for the institution |
| `GET` | `/v1/autobid/rules/{rule_id}` | — | Single rule with version history |
| `GET` | `/v1/autobid/executions` | — | Auto-bid execution log |

## Pipelines

### `app/api/pipeline.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/pipelines` | — | Active loan pipelines |
| `GET` | `/pipelines/{pipeline_id}` | — | Pipeline with stages and the Phase 2 identity reveal |
| `POST` | `/pipelines/{pipeline_id}/stages/{stage_id}/advance` | — | Move a stage forward (maker) |
| `POST` | `/pipelines/{pipeline_id}/stages/{stage_id}/approve` | — | Approve a stage awaiting checker sign-off |

### `app/api/pipeline_templates.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/pipelines/templates` | — | Templates |
| `POST` | `/pipelines/templates` | — | Create a template |
| `GET` | `/pipelines/templates/{template_id}` | — | Template with stage definitions |
| `PUT` | `/pipelines/templates/{template_id}` | — | Update template metadata |
| `POST` | `/pipelines/templates/{template_id}/stages` | — | Append a stage definition |
| `PUT` | `/pipelines/templates/{template_id}/stages/{stage_id}` | — | Update a stage definition |
| `DELETE` | `/pipelines/templates/{template_id}/stages/{stage_id}` | — | Remove a stage definition |

## Approvals & dual control

### `app/api/approvals.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/approvals/pending` | — | Maker-checker queue. `?scope=bids|internal` partitions it |
| `POST` | `/approvals/submit` | — | Submit an action for four-eyes approval |
| `POST` | `/approvals/{action_id}/approve` | — | Approve (checker must differ from maker) |
| `POST` | `/approvals/{action_id}/reject` | — | Reject with a note |
| `POST` | `/approvals/{action_id}/execute-update` | — | Execute an approved user change |
| `POST` | `/approvals/{action_id}/provision-user` | — | Provision a member from an approved action |

### `app/api/approval_engine.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/approval-engine/committees` | — | Approval committees |
| `POST` | `/approval-engine/committees` | — | Create a committee with quorum and tie-break rules |
| `POST` | `/approval-engine/committees/{committee_id}/members` | — | Add a member (voting or observer) |
| `DELETE` | `/approval-engine/committees/{committee_id}/members/{member_row_id}` | — | End a membership (sets `valid_to`) |
| `GET` | `/approval-engine/delegations` | — | Delegations of authority |
| `POST` | `/approval-engine/delegations` | — | Delegate approval authority for a bounded period |
| `DELETE` | `/approval-engine/delegations/{delegation_id}` | — | Revoke a delegation early |
| `GET` | `/approval-engine/templates` | — | Templates |
| `POST` | `/approval-engine/templates` | — | Create a template |
| `POST` | `/approval-engine/templates/{template_id}/activate` | — | Publish a draft approval template |
| `GET` | `/approval-engine/doa-rules` | — | Delegation-of-authority routing rules |
| `POST` | `/approval-engine/doa-rules` | — | Create a routing rule (lowest priority number wins) |
| `POST` | `/approval-engine/doa-rules/simulate` | — | Dry-run routing without creating an instance |
| `POST` | `/approval-engine/route` | — | Route an entity into an approval chain |
| `GET` | `/approval-engine/inbox` | — | Stages awaiting the caller's action, incl. delegated |
| `POST` | `/approval-engine/instances/{instance_id}/actions` | — | Approve / reject / abstain on a stage |
| `POST` | `/approval-engine/instances/{instance_id}/withdraw` | — | Withdraw an in-flight approval |
| `GET` | `/approval-engine/instances/{instance_id}` | — | Full timeline for an instance |
| `GET` | `/approval-engine/analytics` | — | Cycle times, bottlenecks, SLA breaches |

## Documents & e-signature

### `app/api/doc_templates/router.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/institution/doc-templates` | — | Templates |
| `POST` | `/institution/doc-templates` | — | Create a template |
| `PATCH` | `/institution/doc-templates/{template_id}` | — | Update template metadata |
| `POST` | `/institution/doc-templates/{template_id}/retire` | — | Retire a document template |
| `GET` | `/institution/doc-templates/{template_id}/versions` | — | Versions of a document template |
| `POST` | `/institution/doc-templates/{template_id}/versions` | — | Upload a new .docx version (draft) |
| `POST` | `/institution/doc-templates/{template_id}/versions/{version_id}/decide` | — | Approve or reject a version — author cannot approve their own |
| `GET` | `/institution/doc-templates/merge-fields` | — | Available `{{ field }}` merge tags |
| `POST` | `/institution/doc-templates/{template_id}/generate` | — | Generate .docx/.pdf against a deal snapshot |
| `GET` | `/institution/doc-templates/generations` | — | Documents generated for a deal |
| `GET` | `/institution/doc-templates/generations/{generation_id}/download` | — | Download a generated document |

### `app/api/documents.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/documents/types` | — | Compliance document types |
| `GET` | `/documents/compliance` | — | Compliance rollup incl. `can_bid` and missing documents |
| `GET` | `/documents` | — | Institution compliance library |
| `POST` | `/documents` | — | Register an uploaded document |
| `POST` | `/documents/{doc_id}/review` | — | Approve or reject a document |
| `POST` | `/documents/upload-url` | — | Signed upload URL for the institution-docs bucket |

### `app/api/esign.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `POST` | `/esign/envelopes` | — | Create a signature envelope (accepts `doc_generation_id`, `approval_instance_id`) |
| `GET` | `/esign/envelopes` | — | Envelopes with status |
| `GET` | `/esign/envelopes/{envelope_id}/events` | — | Hash-chained audit trail for an envelope |
| `GET` | `/esign/envelopes/{envelope_id}/sealed-url` | — | Signed URL for the sealed PDF |
| `GET` | `/esign/public/{token}` | — | Public: signing ceremony state for a token |
| `POST` | `/esign/public/{token}/otp` | — | Public: send the signer OTP |
| `POST` | `/esign/public/{token}/otp/verify` | — | Public: verify the OTP |
| `POST` | `/esign/public/{token}/sign` | — | Public: record the signature |
| `POST` | `/esign/public/{token}/decline` | — | Public: decline to sign |

## Institution administration

### `app/api/institutions.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/institutions/me` | — | Caller's institution profile |

### `app/api/members.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/members/me` | — | Caller's member record and role |
| `GET` | `/members/my-group` | — | Caller's group and module permissions |
| `GET` | `/members/my-group-debug` | — | Group resolution diagnostics |
| `GET` | `/members` | — | Institution members |
| `GET` | `/members/pending` | — | Member changes awaiting approval |
| `GET` | `/members/{member_id}` | — | Single member |
| `PATCH` | `/members/{member_id}` | — | Update a member (may route to maker-checker) |
| `POST` | `/members/{member_id}/deactivate` | — | Deactivate a member |
| `POST` | `/members/{member_id}/reactivate` | — | Reactivate a member |
| `POST` | `/members/{member_id}/reset-password` | — | Force a password reset |
| `GET` | `/members/{member_id}/audit` | — | Audit trail for one member |

### `app/api/groups.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/groups` | — | Institution groups |
| `GET` | `/groups/pending` | — | Group changes awaiting approval |
| `GET` | `/groups/my-modules` | — | Modules licensed AND permitted for the caller |
| `GET` | `/groups/my-products` | — | Products in the caller's group scope |
| `GET` | `/groups/licensed-products` | — | Products licensed to the institution |

### `app/api/benefits.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/benefits/categories` | — | Benefit categories |
| `GET` | `/benefits` | — | Institution benefits |
| `POST` | `/benefits` | — | Create a benefit |
| `PUT` | `/benefits/{benefit_id}` | — | Update a benefit |
| `DELETE` | `/benefits/{benefit_id}` | — | Deactivate a benefit |

### `app/api/catalog.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/webhooks` | — | Registered webhooks |
| `GET` | `/products` | — | Product catalogue |
| `GET` | `/audit` | — | Institution audit trail |
| `POST` | `/sla-config` | — | Set bid-window and integration overrides |

### `app/api/auth_provision.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `POST` | `/auth/provision-member` | — | Provision a portal login for a member |

### `app/api/notifications.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/notifications/unread-count` | — | Unread notification count |
| `POST` | `/notifications/{notification_id}/mark-read` | — | Mark one notification read |
| `POST` | `/notifications/mark-all-read` | — | Mark all read |

## Integration

### `app/api/api_keys.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/api-keys` | — | API keys (hashes never returned) |
| `POST` | `/api-keys` | — | Create a key — enters maker-checker as pending |
| `PUT` | `/api-keys/{key_id}/approve` | — | Approve and activate a key |
| `POST` | `/api-keys/{key_id}/revoke` | — | Revoke a key |
| `DELETE` | `/api-keys/{key_id}` | — | Hard-delete a key record |

### `app/api/webhooks.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/webhooks` | — | Registered webhooks |
| `POST` | `/webhooks` | — | Register a webhook (endpoint URL is SSRF-validated) |
| `PUT` | `/webhooks/{webhook_id}` | — | Update a webhook |
| `DELETE` | `/webhooks/{webhook_id}` | — | Delete a webhook |
| `POST` | `/webhooks/{webhook_id}/test` | — | Send a test event |
| `GET` | `/webhooks/{webhook_id}/deliveries` | — | Delivery log with attempts and responses |
| `POST` | `/webhooks/{webhook_id}/reset-failures` | — | Reset the failure counter (circuit breaker) |

### `app/api/entitlements.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/v1/entitlements/me` | — | Modules licensed to the institution |
| `GET` | `/v1/entitlements/usage` | — | Metered usage against entitlements |

## Platform admin

### `app/api/admin.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/admin/metrics` | — | Platform KPIs |
| `GET` | `/admin/users` | — | All portal users |
| `GET` | `/admin/roles` | — | Admin roles |
| `GET` | `/admin/sessions` | — | Active sessions |
| `GET` | `/admin/dual-control` | — | Platform dual-control queue |
| `GET` | `/admin/audit` | — | Platform audit trail |
| `GET` | `/admin/institutions` | — | All institutions with onboarding state |
| `GET` | `/admin/user-groups` | — | Group definitions |
| `POST` | `/admin/dual-control/submit` | — | Submit a platform action for approval |
| `POST` | `/admin/dual-control/approve` | — | Approve a platform action |
| `POST` | `/admin/dual-control/reject` | — | Reject a platform action |
| `POST` | `/admin/sessions/terminate` | — | Force-terminate a session |
| `GET` | `/admin/documents` | — | KYB documents pending review |
| `POST` | `/admin/documents/{doc_id}/review` | — | Approve or reject a KYB document |

## Server-to-server (no JWT)

### `app/api/public.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/public/requests/{request_id}/bids` | — | Bids for one request (called by the borrower app) |
| `POST` | `/public/requests/bids/bulk` | — | Bids for many requests in one call |
| `POST` | `/public/requests/{request_id}/accept-bid` | — | Accept a bid — triggers Phase 2 reveal and pipeline creation |
| `GET` | `/public/requests/{request_id}/pipeline` | — | Borrower-visible pipeline stages only |
| `GET` | `/public/market-intelligence` | — | Aggregated market rate intelligence |

## Versioned public API

### `app/api/v1/marketplace.py`

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/v1/requests` | — | Anonymised (Phase 1) requests visible to the caller's institution |
| `GET` | `/v1/bids` | — | Bids (API key auth) |
| `POST` | `/v1/bids` | — | Place a bid. Rejected outside the open bid window |
| `PUT` | `/v1/pipeline/{loan_id}/stages/{stage_id}/advance` | — | Advance a pipeline stage (API key auth) |
| `GET` | `/v1/analytics/summary` | — | Win rate, volume, deal value (API key auth) |

## Borrower app serverless functions (`ficium/api/*.ts`, Vercel)

**12 root-level functions — exactly at the plan ceiling.** Adding a thirteenth breaks the deploy. Shared code lives in `api/_lib/` and `api/_kyc/`, which are not counted as functions.

| Function | Purpose |
|---|---|
| `/api/accept-bid` | Accept a bid — proxies `/public/requests/{id}/accept-bid` |
| `/api/chat` | FICO advisor streaming chat (SSE) |
| `/api/intelligence` | Market intelligence for the Markets module |
| `/api/internal` | Internal cron and maintenance handlers |
| `/api/keepalive` | Warms the Railway API to reduce cold starts |
| `/api/kyc` | KYC — Rekognition face match, liveness, MRZ, Claude Vision extraction |
| `/api/market` | Market data refresh |
| `/api/rate-applicant` | Calls the rating engine for risk tier and score |
| `/api/request-actions` | Request lifecycle actions |
| `/api/request-bids` | Bids for one request |
| `/api/request-bids-bulk` | Bids for many requests in one call |
| `/api/request-builder` | AI-assisted request construction |

## Webhooks (outbound)

Institutions register endpoints under Settings. Deliveries are signed HMAC-SHA256 over the raw body, retried with exponential backoff up to `retry_max`, and logged in full to `institution.webhook_delivery`. A rising `failure_count` trips a circuit breaker; `POST /webhooks/{id}/reset-failures` clears it. Endpoint URLs are SSRF-validated at registration (`app/core/ssrf.py`).

---

_153 portal API endpoints, 12 borrower serverless functions._
