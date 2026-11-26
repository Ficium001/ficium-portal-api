"""Calling a bank's own decision engine (autobid decision_mode external /
rules_then_external): the open-banking side of autobid.

Ficium POSTs the anonymous request object to the HTTPS address the bank set up;
the bank's system answers bid or decline within its timeout. Contract v1
(docs/AUTOBID_DECISION_API.md has the full text for banks):

  POST <engine_url>
  Content-Type: application/json
  Ficium-Event: autobid.decision_request
  Ficium-Decision-Id: <uuid>           (same id on a retry; answer idempotently)
  Ficium-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>

  { "contract": "autobid.decision.v1", "decision_id", "institution_id",
    "request": {request object}, "rule": {id, version, name, suggested_bid} | null,
    "respond_by": ISO time }

  200  {"decision": "bid", "rate_pct", "rate_type", "amount", "term_months",
        "rate_valid_days"?, "conditions"?, "reference"?}
  200  {"decision": "decline", "reason_code"?, "reference"?}

Anything else (timeout, non-2xx, bad JSON, values out of range) is "unavailable":
the engine then skips the request, or uses the rule's price if the bank chose
engine_failure_mode = use_rule. Ficium checks the answer: a rate in (0, 100], an
amount above 0 and never more than was requested, a term from 1 to 480 months.
The bank's daily caps and Ficium's compliance gate still apply after the answer.

The address is checked by the same SSRF guard as webhooks (public HTTPS only) at
setup and before every call; redirects are not followed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from . import api_traffic
from .ssrf import WebhookUrlError, validate_webhook_url

log = structlog.get_logger(__name__)

CONTRACT = "autobid.decision.v1"
CHANNEL = "autobid.decision_out"
MAX_RESPONSE_BYTES = 16_384


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


@dataclass(frozen=True)
class EngineAnswer:
    decision: str  # "bid" | "decline" | "unavailable"
    rate_pct: float | None = None
    rate_type: str | None = None
    amount: float | None = None
    term_months: int | None = None
    rate_valid_days: int | None = None
    conditions: str | None = None
    reference: str | None = None
    reason: str | None = None  # decline reason_code, or why it was unavailable
    status_code: int | None = None
    latency_ms: int | None = None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_answer(payload: Any, requested_amount: float | None) -> EngineAnswer:
    """Validate the bank's JSON answer. Pure, so the rules are unit-tested."""
    if not isinstance(payload, dict):
        return EngineAnswer("unavailable", reason="answer is not a JSON object")
    ref = payload.get("reference")
    ref = str(ref)[:120] if ref is not None else None
    decision = payload.get("decision")
    if decision == "decline":
        code = payload.get("reason_code")
        return EngineAnswer("decline", reference=ref, reason=str(code)[:80] if code else None)
    if decision != "bid":
        return EngineAnswer("unavailable", reason="decision must be bid or decline")

    rate, amount = _num(payload.get("rate_pct")), _num(payload.get("amount"))
    term, days = payload.get("term_months"), payload.get("rate_valid_days", 14)
    rate_type = payload.get("rate_type", "fixed")
    problems = []
    if rate is None or not 0 < rate <= 100:
        problems.append("rate_pct must be above 0 and at most 100")
    if amount is None or amount <= 0:
        problems.append("amount must be above 0")
    elif requested_amount and amount > requested_amount:
        problems.append("amount is more than the borrower asked for")
    if not isinstance(term, int) or isinstance(term, bool) or not 1 <= term <= 480:
        problems.append("term_months must be whole months from 1 to 480")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
        problems.append("rate_valid_days must be whole days from 1 to 90")
    if rate_type not in ("fixed", "variable"):
        problems.append("rate_type must be fixed or variable")
    cond = payload.get("conditions")
    if cond is not None and (not isinstance(cond, str) or len(cond) > 1000):
        problems.append("conditions must be text of at most 1000 characters")
    if problems:
        return EngineAnswer("unavailable", reason="; ".join(problems), reference=ref)
    return EngineAnswer(
        "bid",
        rate_pct=rate,
        rate_type=rate_type,
        amount=round(float(amount or 0), 2),
        term_months=int(term or 0),
        rate_valid_days=int(days),
        conditions=cond,
        reference=ref,
    )


def build_body(
    decision_id: str,
    institution_id: str,
    request_obj: dict[str, Any],
    rule: dict[str, Any] | None,
    timeout_ms: int,
) -> bytes:
    respond_by = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + timeout_ms / 1000))
    return json.dumps(
        {
            "contract": CONTRACT,
            "decision_id": decision_id,
            "institution_id": institution_id,
            "request": request_obj,
            "rule": rule,
            "respond_by": respond_by,
        },
        separators=(",", ":"),
        default=str,
    ).encode()


def ask(
    url: str,
    secret: str,
    timeout_ms: int,
    institution_id: str,
    request_obj: dict[str, Any],
    rule: dict[str, Any] | None,
    decision_id: str | None = None,
    *,
    event: str = "autobid.decision_request",
) -> EngineAnswer:
    """One call to the bank's engine. Never raises: failures are 'unavailable'."""
    decision_id = decision_id or str(uuid.uuid4())
    if api_traffic.paused(CHANNEL):
        api_traffic.record(CHANNEL, api_traffic.BLOCKED)
        return EngineAnswer("unavailable", reason="paused by Ficium")
    try:
        validate_webhook_url(url)
    except WebhookUrlError as e:
        api_traffic.record(CHANNEL, api_traffic.CLIENT)
        return EngineAnswer("unavailable", reason=f"address refused: {e}")

    body = build_body(decision_id, institution_id, request_obj, rule, timeout_ms)
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Ficium-Autobid/1",
        "Ficium-Event": event,
        "Ficium-Decision-Id": decision_id,
        "Ficium-Signature": sign(secret, ts, body),
    }
    started = time.monotonic()
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_ms / 1000, follow_redirects=False) as client:
            resp = client.post(url, content=body, headers=headers)
        latency = int((time.monotonic() - started) * 1000)
    except httpx.HTTPError as e:
        latency = int((time.monotonic() - started) * 1000)
        api_traffic.record(CHANNEL, api_traffic.NETWORK, t0)
        return EngineAnswer(
            "unavailable", reason=f"no answer: {type(e).__name__}", latency_ms=latency
        )

    if not 200 <= resp.status_code < 300:
        api_traffic.record(CHANNEL, api_traffic.outcome_for_status(resp.status_code), t0)
        return EngineAnswer(
            "unavailable",
            reason=f"HTTP {resp.status_code}",
            status_code=resp.status_code,
            latency_ms=latency,
        )
    if len(resp.content) > MAX_RESPONSE_BYTES:
        api_traffic.record(CHANNEL, api_traffic.SERVER, t0)
        return EngineAnswer(
            "unavailable",
            reason="answer too large",
            status_code=resp.status_code,
            latency_ms=latency,
        )
    try:
        payload = resp.json()
    except ValueError:
        api_traffic.record(CHANNEL, api_traffic.SERVER, t0)
        return EngineAnswer(
            "unavailable",
            reason="answer is not JSON",
            status_code=resp.status_code,
            latency_ms=latency,
        )

    requested = request_obj.get("facts", {}).get("amount")
    answer = parse_answer(
        payload, float(requested) if isinstance(requested, (int, float)) else None
    )
    api_traffic.record(
        CHANNEL, api_traffic.OK if answer.decision != "unavailable" else api_traffic.SERVER, t0
    )
    return EngineAnswer(
        **{**answer.__dict__, "status_code": resp.status_code, "latency_ms": latency}
    )
