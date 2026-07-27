# =============================================================================
# Unit tests — app/core/autobid_rules.py
#
# DB-free. Covers:
#   * closed-vocabulary validation (fields, operators, structure, limits)
#   * evaluation semantics incl. rating-band ordering and fail-closed on
#     missing facts
#   * deterministic winner selection (priority → approved_at → rule_id)
# The evaluator here is the SAME code the worker will run — these tests are
# the executable contract for spec §2.3.
# =============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.autobid_rules import (
    CandidateRule,
    RuleValidationError,
    matches,
    select_winning_rule,
    validate_action,
    validate_guardrails,
    validate_predicate,
)

GOOD_PREDICATE: dict[str, Any] = {
    "all": [
        {"field": "rating_band", "op": "in", "value": ["AAA", "AA", "A"]},
        {"field": "amount_mur", "op": "between", "value": [50000, 750000]},
        {"field": "tenor_months", "op": "lte", "value": 48},
        {"field": "product", "op": "eq", "value": "personal_unsecured"},
    ]
}

GOOD_ACTION = {"apr_pct": 11.9, "lane": "soft_pull", "bid_validity_hours": 48, "fee_schedule": "F2"}
GOOD_GUARDRAILS = {"max_bids_per_day": 200, "max_principal_per_day_mur": 5_000_000}

FACTS = {
    "rating_band": "AA",
    "amount_mur": 300000,
    "tenor_months": 36,
    "product": "personal_unsecured",
    "secured": False,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_valid_payloads_pass() -> None:
    validate_predicate(GOOD_PREDICATE)
    validate_action(GOOD_ACTION)
    validate_guardrails(GOOD_GUARDRAILS)


@pytest.mark.parametrize(
    "bad",
    [
        {"field": "salary", "op": "gte", "value": 1},            # unknown field
        {"field": "amount_mur", "op": "regex", "value": ".*"},   # unknown operator
        {"field": "amount_mur", "op": "between", "value": [9, 1]},   # lo > hi
        {"field": "amount_mur", "op": "between", "value": [1]},      # not a pair
        {"field": "rating_band", "op": "between", "value": [0, 1]},  # between on band
        {"field": "rating_band", "op": "in", "value": ["AAA", "ZZ"]},  # unknown band
        {"field": "rating_band", "op": "in", "value": []},        # empty list
        {"field": "secured", "op": "lte", "value": 1},            # lte on bool
        {"field": "amount_mur", "op": "eq", "value": "big"},      # wrong type
        {"field": "amount_mur", "op": "gte", "value": True},      # bool is not numeric
        {"all": []},                                              # empty logical list
        {"all": [{"field": "amount_mur", "op": "gte", "value": 1}], "any": []},  # 2 keys
        "not-an-object",
        {"field": "amount_mur", "op": "gte", "value": 1, "extra": 1},  # unexpected key
    ],
)
def test_invalid_predicates_rejected(bad: Any) -> None:
    with pytest.raises(RuleValidationError):
        validate_predicate(bad)


def test_depth_limit_enforced() -> None:
    node: dict[str, Any] = {"field": "amount_mur", "op": "gte", "value": 1}
    for _ in range(10):
        node = {"not": node}
    with pytest.raises(RuleValidationError, match="depth"):
        validate_predicate(node)


def test_condition_count_limit_enforced() -> None:
    tree = {"all": [{"field": "tenor_months", "op": "lte", "value": 48}] * 41}
    with pytest.raises(RuleValidationError, match="conditions"):
        validate_predicate(tree)


@pytest.mark.parametrize(
    "bad_action",
    [
        {},                                                        # missing everything
        {**GOOD_ACTION, "apr_pct": 0},                             # apr out of range
        {**GOOD_ACTION, "apr_pct": 101},
        {**GOOD_ACTION, "lane": "no_pull"},                        # unknown lane
        {**GOOD_ACTION, "bid_validity_hours": 0},
        {**GOOD_ACTION, "bid_validity_hours": 200},
        {**GOOD_ACTION, "surprise": 1},                            # unexpected key
    ],
)
def test_invalid_actions_rejected(bad_action: dict[str, Any]) -> None:
    with pytest.raises(RuleValidationError):
        validate_action(bad_action)


@pytest.mark.parametrize(
    "bad_gr",
    [
        {},
        {**GOOD_GUARDRAILS, "max_bids_per_day": 0},
        {**GOOD_GUARDRAILS, "max_principal_per_day_mur": -1},
        {**GOOD_GUARDRAILS, "min_seconds_between_bids": -5},
        {**GOOD_GUARDRAILS, "unexpected": True},
    ],
)
def test_invalid_guardrails_rejected(bad_gr: dict[str, Any]) -> None:
    with pytest.raises(RuleValidationError):
        validate_guardrails(bad_gr)


# ---------------------------------------------------------------------------
# Evaluation semantics
# ---------------------------------------------------------------------------

def test_matching_facts_match() -> None:
    assert matches(GOOD_PREDICATE, FACTS) is True


def test_non_matching_amount_fails() -> None:
    assert matches(GOOD_PREDICATE, {**FACTS, "amount_mur": 1_000_000}) is False


def test_missing_fact_fails_closed() -> None:
    facts = dict(FACTS)
    del facts["rating_band"]
    assert matches(GOOD_PREDICATE, facts) is False


def test_none_fact_fails_closed() -> None:
    assert matches(GOOD_PREDICATE, {**FACTS, "rating_band": None}) is False


def test_rating_band_gte_means_at_least_this_good() -> None:
    pred = {"field": "rating_band", "op": "gte", "value": "A"}
    assert matches(pred, {"rating_band": "AAA"}) is True   # better than A
    assert matches(pred, {"rating_band": "A"}) is True     # exactly A
    assert matches(pred, {"rating_band": "BBB"}) is False  # worse than A


def test_rating_band_lte_means_this_or_worse() -> None:
    pred = {"field": "rating_band", "op": "lte", "value": "BBB"}
    assert matches(pred, {"rating_band": "BB"}) is True
    assert matches(pred, {"rating_band": "A"}) is False


def test_any_and_not_combinators() -> None:
    pred = {
        "any": [
            {"field": "secured", "op": "eq", "value": True},
            {"not": {"field": "product", "op": "eq", "value": "sme_loan"}},
        ]
    }
    assert matches(pred, {"secured": False, "product": "personal_unsecured"}) is True
    assert matches(pred, {"secured": False, "product": "sme_loan"}) is False
    assert matches(pred, {"secured": True, "product": "sme_loan"}) is True


# ---------------------------------------------------------------------------
# Deterministic winner selection (spec §2.3)
# ---------------------------------------------------------------------------

T0 = datetime(2026, 7, 1, tzinfo=UTC)
MATCH_ALL = {"field": "tenor_months", "op": "lte", "value": 999}


def cand(rule_id: str, priority: int, approved_offset_days: int) -> CandidateRule:
    return CandidateRule(
        rule_id=rule_id,
        rule_version_id=f"v-{rule_id}",
        priority=priority,
        approved_at=T0 + timedelta(days=approved_offset_days),
        predicate=MATCH_ALL,
    )


def test_lowest_priority_number_wins() -> None:
    winner = select_winning_rule([cand("a", 200, 0), cand("b", 50, 5)], FACTS)
    assert winner is not None and winner.rule_id == "b"


def test_priority_tie_broken_by_oldest_approval() -> None:
    winner = select_winning_rule([cand("newer", 100, 9), cand("older", 100, 1)], FACTS)
    assert winner is not None and winner.rule_id == "older"


def test_full_tie_broken_by_rule_id_total_order() -> None:
    winner = select_winning_rule([cand("zzz", 100, 1), cand("aaa", 100, 1)], FACTS)
    assert winner is not None and winner.rule_id == "aaa"


def test_no_match_returns_none() -> None:
    only = CandidateRule(
        rule_id="x",
        rule_version_id="v-x",
        priority=1,
        approved_at=T0,
        predicate={"field": "amount_mur", "op": "gte", "value": 10_000_000},
    )
    assert select_winning_rule([only], FACTS) is None


def test_non_matching_high_priority_rule_does_not_shadow() -> None:
    non_matching = CandidateRule(
        rule_id="strict",
        rule_version_id="v-s",
        priority=1,
        approved_at=T0,
        predicate={"field": "amount_mur", "op": "gte", "value": 10_000_000},
    )
    winner = select_winning_rule([non_matching, cand("fallback", 500, 0)], FACTS)
    assert winner is not None and winner.rule_id == "fallback"
