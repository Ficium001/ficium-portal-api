# Autobid decision API (contract `autobid.decision.v1`)

For banks that want **their own systems** to decide and price Ficium bids. Ficium
sends each new request that fits your autobid setting to an HTTPS address you
run; your system answers *bid* or *decline*. Nothing about the borrower that
could identify them is ever sent.

You choose how this fits your process, in the portal under **Autobid › Setup**
(every change is approved by a second person at your bank):

| Setting | Options |
|---|---|
| Who decides | **Ficium rules** (no call to you) · **Your decision engine** (every eligible request is sent to you) · **Rules, then your engine** (your approved rules pick which requests to send; the winning rule's price comes along as `suggested_bid`) |
| Approval | **A person approves every auto-bid** (your answer becomes a bid waiting in your bid approvals) · **Send at once** (your answer is the bid, within your daily limits) |
| If your engine does not answer | Skip the request, or (rules then engine only) use the rule's price |
| Limits | Bids a day and amount a day for the whole bank; rules have their own |
| Products | Every product you enabled, or a list |

Pulling instead of being called is also supported: `GET /v1/requests` returns the
same request object, and `POST /v1/bids` places a bid (API key with
`marketplace:read` / `bids:write`).

## The call

```
POST <your engine address>
Content-Type: application/json
User-Agent: Ficium-Autobid/1
Ficium-Event: autobid.decision_request        (autobid.decision_test for "Send a test request")
Ficium-Decision-Id: 6f1c…                       (the same id if Ficium ever asks again: answer the same way)
Ficium-Signature: t=1764150000,v1=5d2a…
```

```json
{
  "contract": "autobid.decision.v1",
  "decision_id": "6f1c…",
  "institution_id": "f192…",
  "respond_by": "2026-11-26T12:00:05Z",
  "request": {
    "request_id": "8a3e…",
    "product_type": "personal_loan",
    "status": "open",
    "bid_window_closes_at": "2026-11-29T12:00:00+00:00",
    "created_at": "2026-11-26T12:00:00+00:00",
    "allocation_mode": null,
    "facts": {
      "product": "personal_loan", "amount": 500000, "term_months": 60, "currency": "MUR",
      "risk_tier": "B", "ficium_score": 71, "kyc_verified": true, "income_verified": true,
      "employment_status": "employed", "gross_monthly_income": 85000, "dsr_post_pct": 32, "age": 38
    }
  },
  "rule": null
}
```

`facts` holds only what the borrower released and what is present; a missing key
means Ficium does not know it. Possible keys: `product`, `amount`, `term_months`,
`currency`, `risk_tier` (A best … D), `ficium_score`, `kyc_verified`,
`income_verified`, `employment_status`, `employment_type`, `years_employed`,
`gross_monthly_income`, `dsr_current_pct`, `dsr_post_pct`, `has_existing_loans`,
`existing_monthly_repayment`, `net_worth_band`, `affordability_score`, `age`,
`collateral_type`, `ltv_pct`, `risk_appetite`, `investment_horizon`,
`liquidity_pref`, `target_amount`, `monthly_contribution`.

With *Rules, then your engine*, `rule` is
`{"id", "version", "name", "suggested_bid": {"rate_pct", "rate_type", "amount", "term_months", "rate_valid_days", "conditions"}}`.

## Your answer (HTTP 200, within your timeout: 500–10,000 ms, default 5,000)

```json
{"decision": "bid", "rate_pct": 10.9, "rate_type": "fixed", "amount": 450000,
 "term_months": 60, "rate_valid_days": 14, "conditions": "Salary domiciliation", "reference": "LOS-88213"}
```

```json
{"decision": "decline", "reason_code": "POLICY_DTI", "reference": "LOS-88214"}
```

| Field | Rule |
|---|---|
| `rate_pct` | above 0, at most 100 (% a year) |
| `amount` | above 0, never more than the borrower asked for |
| `term_months` | whole months, 1–480 |
| `rate_type` | `fixed` (default) or `variable` |
| `rate_valid_days` | 1–90 (default 14) |
| `conditions` | optional, up to 1,000 characters, shown to the borrower |
| `reference` | optional, up to 120 characters: your id, shown in Autobid › Activity |

Anything else (a timeout, a non-2xx status, invalid JSON, a value out of range, a
redirect) counts as **no answer**. After your answer Ficium still applies your daily
limits and its compliance check (your bank's documents must be approved and in date).

## Verify the signature

Ficium signs `"<t>.<raw body>"` with HMAC-SHA256 and the signing secret shown once
when your engine was approved (Autobid › Setup; an admin can replace it). Reject a
request whose signature does not match or whose `t` is more than 5 minutes old.

```python
import hashlib, hmac, time

def verified(secret: str, header: str, raw_body: bytes, tolerance_s: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    t, v1 = int(parts["t"]), parts["v1"]
    if abs(time.time() - t) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)
```

```js
const crypto = require('crypto')
function verified(secret, header, rawBody, toleranceS = 300) {
  const parts = Object.fromEntries(header.split(',').map(p => p.split('=')))
  const t = Number(parts.t)
  if (Math.abs(Date.now() / 1000 - t) > toleranceS) return false
  const expected = crypto.createHmac('sha256', secret).update(`${t}.`).update(rawBody).digest('hex')
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1))
}
```

## Safety

- Your address must be public `https`. Ficium refuses private, loopback and
  link-local addresses when it is set and again before every call, and never
  follows redirects.
- Ficium can pause all decision-engine calls platform-wide (API traffic channel
  `autobid.decision_out`); your bank can stop autobid at once (Autobid › Overview).
- One evaluation per request per bank, ever: a retry can never produce a second bid.
