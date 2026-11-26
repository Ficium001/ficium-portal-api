"""Autobid engine pieces that need no database: facts, the bank decision contract,
engine helpers, and the per-bank setting validation."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.api.autobid import _DEFAULTS, validate_config
from app.core import autobid_decision
from app.core.autobid_decision import parse_answer, sign
from app.core.autobid_engine import active_rules, caps_allow, pick_rule
from app.core.autobid_facts import build_facts, request_object

ROW: dict[str, Any] = {
    "id": "11111111-1111-4111-8111-111111111111",
    "product_type": "personal_loan",
    "amount": Decimal("500000.00"),
    "term_months": 60,
    "currency": "MUR",
    "status": "bidding",
    "bid_window_closes_at": datetime(2026, 12, 1, tzinfo=UTC),
    "created_at": datetime(2026, 11, 26, tzinfo=UTC),
    "allocation_mode": None,
    "ficium_risk_tier": None,
    "ficium_score": None,
    "params": {"collateral_type": "none", "ltv_pct": "0"},
    "metadata": {
        "risk_tier": "b",
        "risk_score": "71",
        "kyc_verified": True,
        "income_verified": "true",
        "gross_monthly_income": "85,000",
        "dsr_post_pct": "32%",
        "age": 38,
        "employment_status": "employed",
        "employer": "MUST NOT LEAK",
        "years_employed": "x",
    },
    "consumer_id": "22222222-2222-4222-8222-222222222222",
}


# ── facts ───────────────────────────────────────────────────────────────────


def test_facts_are_coerced_and_minimal() -> None:
    f = build_facts(ROW)
    assert (
        f["product"] == "personal_loan" and f["amount"] == 500000 and isinstance(f["amount"], int)
    )
    assert f["risk_tier"] == "B" and f["ficium_score"] == 71
    assert (
        f["income_verified"] is True
        and f["gross_monthly_income"] == 85000
        and f["dsr_post_pct"] == 32
    )
    assert "years_employed" not in f  # unreadable value left out, never guessed
    assert "employer" not in f


def test_request_object_never_carries_borrower_ids() -> None:
    body = json.dumps(request_object(ROW), default=str)
    assert "22222222" not in body and "MUST NOT LEAK" not in body
    assert request_object(ROW)["status"] == "open"


# ── the bank's decision engine ──────────────────────────────────────────────


def test_signature_is_hmac_over_timestamp_and_body() -> None:
    body = b'{"a":1}'
    sig = sign("s3cret", 1700000000, body)
    t, v1 = sig.split(",")
    expected = hmac.new(b"s3cret", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert t == "t=1700000000" and v1 == f"v1={expected}"


def test_parse_bid_and_decline() -> None:
    a = parse_answer(
        {
            "decision": "bid",
            "rate_pct": 10.5,
            "amount": 400000,
            "term_months": 48,
            "reference": "LOS-9",
        },
        500000,
    )
    assert (
        a.decision == "bid"
        and a.rate_type == "fixed"
        and a.rate_valid_days == 14
        and a.reference == "LOS-9"
    )
    d = parse_answer({"decision": "decline", "reason_code": "POLICY_DTI"}, 500000)
    assert d.decision == "decline" and d.reason == "POLICY_DTI"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"decision": "maybe"},
        {"decision": "bid", "rate_pct": 0, "amount": 1, "term_months": 12},
        {"decision": "bid", "rate_pct": 9, "amount": 600000, "term_months": 12},  # more than asked
        {"decision": "bid", "rate_pct": 9, "amount": 1, "term_months": 481},
        {"decision": "bid", "rate_pct": 9, "amount": 1, "term_months": 12, "rate_type": "tracker"},
        {"decision": "bid", "rate_pct": 9, "amount": 1, "term_months": 12, "rate_valid_days": 0},
    ],
)
def test_bad_answers_are_unavailable(payload: Any) -> None:
    assert parse_answer(payload, 500000).decision == "unavailable"


def test_ask_refuses_private_address_without_calling(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: called.append("x"))
    a = autobid_decision.ask(
        "https://127.0.0.1/decide", "s", 1000, "iid", request_object(ROW), None
    )
    assert a.decision == "unavailable" and "address refused" in (a.reason or "") and not called


def test_ask_signs_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(autobid_decision, "validate_webhook_url", lambda url: None)

    def fake_post(
        self: httpx.Client, url: str, content: bytes, headers: dict[str, str]
    ) -> httpx.Response:
        seen.update(url=url, body=content, headers=headers)
        return httpx.Response(
            200,
            json={"decision": "bid", "rate_pct": 9.5, "amount": 450000, "term_months": 60},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    a = autobid_decision.ask(
        "https://engine.bank.example/decide",
        "k",
        2000,
        "iid",
        request_object(ROW),
        None,
        decision_id="d-1",
    )
    assert a.decision == "bid" and a.amount == 450000 and a.status_code == 200
    h = seen["headers"]
    assert h["Ficium-Decision-Id"] == "d-1" and h["Ficium-Event"] == "autobid.decision_request"
    ts = int(h["Ficium-Signature"].split(",")[0][2:])
    assert h["Ficium-Signature"] == sign("k", ts, seen["body"])
    sent = json.loads(seen["body"])
    assert (
        sent["contract"] == "autobid.decision.v1" and sent["request"]["facts"]["amount"] == 500000
    )


def test_ask_non_2xx_and_timeouts_are_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autobid_decision, "validate_webhook_url", lambda url: None)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda self, url, **k: httpx.Response(503, request=httpx.Request("POST", url)),
    )
    assert (
        autobid_decision.ask("https://e.example", "k", 1000, "i", request_object(ROW), None).reason
        == "HTTP 503"
    )

    def boom(self: httpx.Client, url: str, **k: Any) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx.Client, "post", boom)
    a = autobid_decision.ask("https://e.example", "k", 1000, "i", request_object(ROW), None)
    assert a.decision == "unavailable" and "ReadTimeout" in (a.reason or "")


# ── engine helpers ──────────────────────────────────────────────────────────


def test_expired_rules_are_dropped() -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    rows = [
        {"guardrails": {"expires_at": "2026-11-25"}},
        {"guardrails": {"expires_at": "2026-12-01T00:00:00+00:00"}},
        {"guardrails": {}},
        {"guardrails": {"expires_at": "nonsense"}},
    ]
    assert len(active_rules(rows, now)) == 2


def test_pick_rule_uses_priority() -> None:
    t = datetime(2026, 11, 1, tzinfo=UTC)
    rows = [
        {
            "rule_id": "r1",
            "rule_version_id": "v1",
            "priority": 50,
            "approved_at": t,
            "predicate": {"field": "product", "op": "eq", "value": "personal_loan"},
        },
        {
            "rule_id": "r2",
            "rule_version_id": "v2",
            "priority": 10,
            "approved_at": t + timedelta(days=1),
            "predicate": {"field": "product", "op": "in", "value": ["personal_loan", "mortgage"]},
        },
    ]
    assert pick_rule(rows, build_facts(ROW))["rule_id"] == "r2"  # type: ignore[index]


def test_caps() -> None:
    assert caps_allow(0, 0, 100, 1, 1000) is None
    assert "bid limit" in (caps_allow(1, 0, 100, 1, 1000) or "")
    assert "amount limit" in (caps_allow(0, 950, 100, 5, 1000) or "")


# ── per-bank setting validation ─────────────────────────────────────────────


def _cfg(**kw: Any) -> dict[str, Any]:
    return {**_DEFAULTS, **kw}


def test_rules_mode_needs_no_engine() -> None:
    validate_config(_cfg(enabled=True))


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"decision_mode": "external"}, "decision engine"),
        ({"engine_failure_mode": "use_rule"}, "rules_then_external"),
        ({"approval_mode": "auto"}, "approval_mode"),
        ({"max_bids_per_day": 0}, "max_bids_per_day"),
        ({"max_amount_per_day": -1}, "max_amount_per_day"),
        ({"product_codes": ["personal_unsecured"]}, "product_codes"),
        ({"product_codes": []}, "product_codes"),
        ({"engine_timeout_ms": 20000}, "engine_timeout_ms"),
        (
            {"decision_mode": "external", "engine_url": "http://bank.example/decide"},
            "engine_url refused",
        ),
        (
            {"decision_mode": "external", "engine_url": "https://10.0.0.5/decide"},
            "engine_url refused",
        ),
    ],
)
def test_bad_configs(kw: dict[str, Any], msg: str) -> None:
    with pytest.raises(HTTPException) as e:
        validate_config(_cfg(**kw))
    assert msg in str(e.value.detail)
