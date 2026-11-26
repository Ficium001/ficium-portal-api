"""app/core/autobid_rules.py: the rule language. DB-free; the engine runs this same code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.autobid_rules import (
    CandidateRule,
    RuleValidationError,
    evaluate,
    matches,
    price,
    select_winning_rule,
    validate_action,
    validate_guardrails,
    validate_predicate,
    vocabulary,
)

GOOD_PREDICATE: dict[str, Any] = {
    "all": [
        {"field": "product", "op": "eq", "value": "personal_loan"},
        {"field": "risk_tier", "op": "lte", "value": "B"},
        {"field": "amount", "op": "between", "value": [50000, 750000]},
        {"field": "term_months", "op": "lte", "value": 60},
        {"field": "kyc_verified", "op": "eq", "value": True},
    ]
}
GOOD_ACTION: dict[str, Any] = {
    "rate_pct": 11.9,
    "rate_by_tier": {"A": 9.9},
    "rate_type": "fixed",
    "amount": {"mode": "requested"},
    "term": {"mode": "requested"},
    "rate_valid_days": 14,
}
GOOD_GUARDRAILS = {"max_bids_per_day": 50, "max_amount_per_day": 20_000_000}
FACTS: dict[str, Any] = {
    "product": "personal_loan",
    "risk_tier": "B",
    "amount": 300000,
    "term_months": 36,
    "kyc_verified": True,
    "currency": "MUR",
}


def test_valid_payloads_pass() -> None:
    validate_predicate(GOOD_PREDICATE)
    validate_action(GOOD_ACTION)
    validate_guardrails(GOOD_GUARDRAILS)


@pytest.mark.parametrize(
    "bad",
    [
        {"field": "salary", "op": "gte", "value": 1},
        {"field": "amount", "op": "regex", "value": ".*"},
        {"field": "amount", "op": "between", "value": [9, 1]},
        {"field": "amount", "op": "between", "value": [1]},
        {"field": "risk_tier", "op": "between", "value": [0, 1]},
        {"field": "risk_tier", "op": "in", "value": ["A", "Z"]},
        {"field": "product", "op": "in", "value": []},
        {"field": "product", "op": "eq", "value": "personal_unsecured"},
        {"field": "kyc_verified", "op": "lte", "value": 1},
        {"field": "amount", "op": "eq", "value": "big"},
        {"field": "amount", "op": "gte", "value": True},
        {"field": "employment_status", "op": "gte", "value": "employed"},
        {"all": []},
        {"all": [{"field": "amount", "op": "gte", "value": 1}], "any": []},
        "not-an-object",
        {"field": "amount", "op": "gte", "value": 1, "extra": 1},
    ],
)
def test_invalid_predicates_rejected(bad: Any) -> None:
    with pytest.raises(RuleValidationError):
        validate_predicate(
            {"all": [{"field": "product", "op": "eq", "value": "mortgage"}, bad]}
            if isinstance(bad, dict) and "field" in bad
            else bad
        )


def test_rule_must_name_its_products() -> None:
    with pytest.raises(RuleValidationError, match="product"):
        validate_predicate({"field": "amount", "op": "lte", "value": 1_000_000})
    # every branch of an "any" must name products
    with pytest.raises(RuleValidationError, match="product"):
        validate_predicate(
            {
                "any": [
                    {"field": "product", "op": "eq", "value": "mortgage"},
                    {"field": "amount", "op": "lte", "value": 5},
                ]
            }
        )
    validate_predicate(
        {
            "any": [
                {"field": "product", "op": "eq", "value": "mortgage"},
                {"field": "product", "op": "in", "value": ["leasing", "overdraft"]},
            ]
        }
    )


def test_limits_enforced() -> None:
    deep: dict[str, Any] = {"field": "product", "op": "eq", "value": "mortgage"}
    for _ in range(8):
        deep = {"all": [deep]}
    with pytest.raises(RuleValidationError, match="deeper"):
        validate_predicate(deep)
    many = {
        "all": [{"field": "product", "op": "eq", "value": "mortgage"}]
        + [{"field": "amount", "op": "gte", "value": i} for i in range(41)]
    }
    with pytest.raises(RuleValidationError, match="conditions"):
        validate_predicate(many)


@pytest.mark.parametrize(
    "patch",
    [
        {"rate_pct": 0},
        {"rate_pct": 101},
        {"rate_pct": True},
        {"rate_by_tier": {"E": 5}},
        {"rate_by_tier": {"A": 0}},
        {"rate_by_tier": {}},
        {"rate_type": "tracker"},
        {"amount": {"mode": "fixed"}},
        {"amount": {"mode": "pct_of_requested", "value": 120}},
        {"amount": {"mode": "all"}},
        {"term": {"mode": "fixed", "value": 0}},
        {"term": {"mode": "fixed", "value": 12.5}},
        {"rate_valid_days": 0},
        {"rate_valid_days": 91},
        {"conditions": "x" * 1001},
        {"lane": "soft_pull"},
    ],
)
def test_invalid_actions_rejected(patch: dict[str, Any]) -> None:
    with pytest.raises(RuleValidationError):
        validate_action({**GOOD_ACTION, **patch})


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"max_bids_per_day": 0, "max_amount_per_day": 1},
        {"max_bids_per_day": 1, "max_amount_per_day": 0},
        {"max_bids_per_day": 1, "max_amount_per_day": 1, "expires_at": "soon"},
        {"max_bids_per_day": 1, "max_amount_per_day": 1, "x": 1},
    ],
)
def test_invalid_guardrails_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(RuleValidationError):
        validate_guardrails(bad)


def test_match_and_tier_ordering() -> None:
    assert matches(GOOD_PREDICATE, FACTS)
    assert matches(GOOD_PREDICATE, {**FACTS, "risk_tier": "A"})
    assert not matches(GOOD_PREDICATE, {**FACTS, "risk_tier": "C"})
    gte = {"field": "risk_tier", "op": "gte", "value": "C"}  # C or weaker
    assert matches(gte, {"risk_tier": "D"}) and not matches(gte, {"risk_tier": "B"})


def test_missing_fact_never_matches_even_under_not() -> None:
    no_kyc = {k: v for k, v in FACTS.items() if k != "kyc_verified"}
    assert evaluate(GOOD_PREDICATE, no_kyc) is None
    assert not matches(GOOD_PREDICATE, no_kyc)
    not_young = {"not": {"field": "age", "op": "lte", "value": 21}}
    assert not matches(not_young, FACTS)  # no age: unknown, not "not young"
    assert matches(not_young, {**FACTS, "age": 40})
    # an "any" decided by a known branch still matches
    either = {
        "any": [
            {"field": "age", "op": "gte", "value": 30},
            {"field": "product", "op": "eq", "value": "personal_loan"},
        ]
    }
    assert matches(either, FACTS)


def test_wrong_typed_fact_is_unknown() -> None:
    assert evaluate({"field": "amount", "op": "gte", "value": 1}, {"amount": "lots"}) is None
    assert (
        evaluate({"field": "kyc_verified", "op": "eq", "value": True}, {"kyc_verified": "yes"})
        is None
    )


def test_price_requested_and_tier_rate() -> None:
    bid = price(GOOD_ACTION, FACTS)
    assert bid is not None
    assert (bid.rate_pct, bid.amount, bid.term_months, bid.rate_valid_days) == (
        11.9,
        300000,
        36,
        14,
    )
    assert price(GOOD_ACTION, {**FACTS, "risk_tier": "A"}).rate_pct == 9.9  # type: ignore[union-attr]


def test_price_never_offers_more_than_asked() -> None:
    a = {
        **GOOD_ACTION,
        "amount": {"mode": "fixed", "value": 1_000_000},
        "term": {"mode": "fixed", "value": 24},
    }
    bid = price(a, FACTS)
    assert bid is not None and bid.amount == 300000 and bid.term_months == 24
    half = {**GOOD_ACTION, "amount": {"mode": "pct_of_requested", "value": 50}}
    assert price(half, FACTS).amount == 150000  # type: ignore[union-attr]


def test_price_none_when_request_lacks_what_action_needs() -> None:
    assert price(GOOD_ACTION, {**FACTS, "term_months": None}) is None
    assert price(GOOD_ACTION, {k: v for k, v in FACTS.items() if k != "amount"}) is None


def _cand(
    rule_id: str, priority: int, approved: datetime, product: str = "personal_loan"
) -> CandidateRule:
    return CandidateRule(
        rule_id,
        f"v-{rule_id}",
        priority,
        approved,
        {"field": "product", "op": "eq", "value": product},
    )


def test_winner_is_deterministic() -> None:
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    c = [
        _cand("b", 10, t0 + timedelta(days=1)),
        _cand("a", 10, t0),
        _cand("c", 5, t0 + timedelta(days=9)),
        _cand("d", 1, t0, "mortgage"),
    ]
    assert select_winning_rule(c, FACTS).rule_id == "c"  # type: ignore[union-attr]
    assert select_winning_rule(c[:2], FACTS).rule_id == "a"  # type: ignore[union-attr]
    assert select_winning_rule([c[3]], FACTS) is None


def test_vocabulary_lists_every_field() -> None:
    v = vocabulary()
    fields = {f["field"]: f for f in v["fields"]}
    assert fields["risk_tier"]["ops"] == ["eq", "gte", "in", "lte"]
    assert "personal_loan" in v["products"] and v["risk_tiers"] == ["A", "B", "C", "D"]
