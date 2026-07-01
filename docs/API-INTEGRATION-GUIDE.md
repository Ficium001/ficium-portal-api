# Ficium API Integration Guide

_For institution engineering teams (MCB and future partners)_  
_Last updated: 1 July 2026_

---

## Overview

Ficium exposes a versioned REST API at `https://ficium-portal-api-production.up.railway.app/v1/` that lets your internal systems interact with the marketplace programmatically — without a human logging into the portal.

Typical integration patterns:

- **LOS (Loan Origination System)** polls `/v1/requests` for new borrower requests matching your product range, then submits bids via `POST /v1/bids`.
- **Core banking** receives `bid.accepted` webhook events when a borrower accepts your offer, triggers internal loan file creation.
- **Workflow system** calls `PUT /v1/pipeline/.../advance` as each internal stage completes, keeping Ficium's pipeline in sync.
- **Analytics / reporting** pulls `/v1/analytics/summary` on schedule.

---

## 1. Getting an API key

API keys are managed in the Ficium Portal under **Settings → API Keys**.

1. A portal admin (maker) creates a key, specifying a label and the required scopes.
2. A second portal admin (checker) approves the key — maker-checker is enforced, the same user cannot create and approve.
3. The raw key (`fic_live_<64 hex chars>`) is shown **once** at creation. Copy it to your vault (AWS Secrets Manager, HashiCorp Vault, etc.) immediately.
4. The key is now active.

**Key rotation** — to rotate: create a new key, approve it, update your systems, then revoke the old key. There is no grace period.

**Scopes** — request only the scopes your integration needs:

| Scope | What it allows |
|-------|---------------|
| `marketplace:read` | Browse open borrower requests |
| `bids:read` | View your submitted bids |
| `bids:write` | Submit bids |
| `pipeline:read` | View pipeline status |
| `pipeline:write` | Advance pipeline stages |
| `analytics:read` | Pull performance metrics |
| `documents:write` | Upload documents to pipeline stages |

---

## 2. Making requests

All API calls use `Authorization: Bearer <api_key>`:

```http
GET /v1/requests?product_type=personal_loan&amount_min=50000&amount_max=500000
Authorization: Bearer fic_live_a3b4c5d6e7f8...
Content-Type: application/json
```

Base URL: `https://ficium-portal-api-production.up.railway.app`

OpenAPI spec (Swagger UI): `https://ficium-portal-api-production.up.railway.app/docs`

---

## 3. Core workflows

### 3a. Browse and bid on requests

```bash
# 1. Get open requests your institution is eligible to bid on
GET /v1/requests?product_type=personal_loan&limit=20

# Response
{
  "total": 47,
  "requests": [
    {
      "id": "3f8a1b2c-...",
      "product_type": "personal_loan",
      "amount_requested": 250000,
      "term_months": 60,
      "purpose": "home_improvement",
      "ficium_risk_tier": "B",
      "ficium_score": 712,
      "bid_window_closes_at": "2026-07-03T14:00:00Z",
      "already_bid": false
    }
  ]
}

# 2. Submit a bid
POST /v1/bids
{
  "request_id": "3f8a1b2c-...",
  "rate": 8.75,
  "rate_type": "fixed",
  "amount_offered": 250000,
  "term_months": 60,
  "rate_valid_days": 30,
  "conditions": "Subject to satisfactory credit check and income verification."
}

# Response (201)
{
  "id": "bid-uuid",
  "request_id": "3f8a1b2c-...",
  "status": "submitted",
  "submitted_at": "2026-07-01T10:23:00Z",
  "expires_at": "2026-07-31T10:23:00Z"
}
```

### 3b. Advance a pipeline stage

When your LOS completes an internal stage (e.g. credit check done, offer letter issued), call:

```bash
PUT /v1/pipeline/{loan_id}/stages/{stage_id}/advance
{
  "note": "Credit assessment completed. Score: 712. Approved."
}

# Response
{
  "loan_id": "...",
  "stage_id": "...",
  "stage_key": "credit_docs",
  "status": "completed",
  "pipeline_completed": false,
  "next_stage_id": "..."
}
```

Get `loan_id` and `stage_id` from the `bid.accepted` webhook payload (see section 4).

### 3c. Pull analytics

```bash
GET /v1/analytics/summary?days=30

{
  "period_days": 30,
  "bids": {
    "total_bids": 42,
    "accepted_bids": 11,
    "rejected_bids": 18,
    "pending_bids": 13,
    "avg_rate": 9.12,
    "avg_amount": 187500,
    "total_accepted_value": 2062500,
    "acceptance_rate_pct": 26.2
  },
  "pipelines": {
    "total_pipelines": 11,
    "completed_pipelines": 3,
    "active_pipelines": 8,
    "avg_deal_amount": 187500,
    "avg_deal_rate": 8.84,
    "avg_completion_days": 18.5
  }
}
```

---

## 4. Receiving webhooks

Register your endpoint in the Ficium Portal under **Settings → Webhooks**.

Ficium sends a signed HTTP POST to your endpoint for each event. **Verify the signature before processing.**

### Signature verification

```python
import hmac
import hashlib

def verify_ficium_webhook(body: bytes, signature_header: str, signing_secret: str) -> bool:
    """
    signature_header is the value of X-Ficium-Signature-256.
    signing_secret is the value shown when you registered the webhook.
    """
    expected = "sha256=" + hmac.new(
        signing_secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

```javascript
const crypto = require('crypto');

function verifyFiciumWebhook(bodyBuffer, signatureHeader, signingSecret) {
  const expected = 'sha256=' + crypto
    .createHmac('sha256', signingSecret)
    .update(bodyBuffer)
    .digest('hex');
  return crypto.timingSafeEqual(
    Buffer.from(expected),
    Buffer.from(signatureHeader)
  );
}
```

### Webhook headers

```
X-Ficium-Event: bid.accepted
X-Ficium-Delivery: <uuid>          # unique per delivery attempt
X-Ficium-Timestamp: 1751366400    # unix epoch — reject if > 5 min old
X-Ficium-Signature-256: sha256=<hex>
Content-Type: application/json
```

### Event payloads

**`bid.accepted`** — fired when a borrower accepts your bid:
```json
{
  "event_id": "uuid",
  "event_type": "bid.accepted",
  "bid_id": "uuid",
  "request_id": "uuid",
  "loan_id": "uuid",
  "deal_amount": 250000,
  "deal_rate": 8.75,
  "deal_term_months": 60,
  "institution_id": "uuid"
}
```

**`pipeline.stage_changed`** — fired when a stage is advanced (including by your LOS):
```json
{
  "event_id": "uuid",
  "event_type": "pipeline.stage_changed",
  "loan_id": "uuid",
  "stage_id": "uuid",
  "stage_key": "credit_docs",
  "previous_status": "in_progress",
  "new_status": "completed",
  "pipeline_completed": false,
  "next_stage_id": "uuid"
}
```

**`bid.rejected`** — fired when a bid expires or the request closes without acceptance:
```json
{
  "event_id": "uuid",
  "event_type": "bid.rejected",
  "bid_id": "uuid",
  "request_id": "uuid",
  "reason": "request_expired"
}
```

### Responding to webhooks

Return HTTP 2xx within the timeout (default 30s). Any non-2xx or timeout is treated as a failure. Ficium retries with exponential backoff: 5s → 25s → 125s (3 attempts). After 10 consecutive failures your webhook endpoint is auto-disabled; reset it in the portal.

**Idempotency** — use `X-Ficium-Delivery` as an idempotency key. The same delivery may be retried multiple times.

---

## 5. Replay protection

Check `X-Ficium-Timestamp` on every request. Reject if the timestamp is more than 5 minutes old:

```python
import time

def is_timestamp_valid(timestamp_header: str, tolerance_seconds: int = 300) -> bool:
    try:
        ts = int(timestamp_header)
        return abs(time.time() - ts) <= tolerance_seconds
    except (ValueError, TypeError):
        return False
```

---

## 6. Error responses

All errors return standard JSON:

```json
{
  "detail": "API key missing required scope: bids:write"
}
```

| Status | Meaning |
|--------|---------|
| `401` | Missing, invalid, expired, or revoked API key |
| `403` | Key valid but missing required scope |
| `404` | Resource not found |
| `409` | Business logic conflict (bid window closed, duplicate bid, stage not in_progress) |
| `422` | Validation error (missing or invalid fields) |
| `503` | Upstream DB unavailable (retry with backoff) |

---

## 7. Rate limits

No hard rate limits are enforced in the current release. Please keep polling intervals to at least 60 seconds for `/v1/requests`. Use webhooks instead of polling wherever possible.

---

## 8. Environments

| Environment | Base URL |
|-------------|---------|
| Production | `https://ficium-portal-api-production.up.railway.app` |
| Sandbox | _(coming soon — contact Ficium)_ |

---

## 9. Support

Technical integration support: **kishan.jeebun@ficium.net**

OpenAPI spec: `GET /openapi.json` (machine-readable, importable into Postman/Insomnia)
