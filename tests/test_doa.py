"""DoA evaluator invariants — pure-function tests, no DB."""
from decimal import Decimal

import pytest

from app.core.doa import DoaRule, EntityParams, NoMatchingRule, evaluate


def rule(pri, cond, tid="tpl", et="bid"):
    return DoaRule(id=f"r{pri}", entity_type=et, priority=pri,
                   conditions=cond, template_id=tid)


CATCH_ALL = rule(9999, {}, tid="default")


def test_first_priority_match_wins():
    rules = [
        rule(10, {"amount_max": 2_000_000, "secured": False}, tid="fast"),
        rule(20, {"amount_max": 10_000_000}, tid="committee"),
        CATCH_ALL,
    ]
    p = EntityParams("bid", amount=Decimal("1500000"), secured=False)
    assert evaluate(rules, p).template_id == "fast"


def test_secured_falls_through_to_heavier_chain():
    rules = [
        rule(10, {"amount_max": 2_000_000, "secured": False}, tid="fast"),
        rule(20, {"secured": True}, tid="legal_chain"),
        CATCH_ALL,
    ]
    p = EntityParams("bid", amount=Decimal("1500000"), secured=True)
    assert evaluate(rules, p).template_id == "legal_chain"


def test_amount_band_boundaries_inclusive():
    rules = [rule(10, {"amount_min": 1000, "amount_max": 5000}, tid="band"), CATCH_ALL]
    assert evaluate(rules, EntityParams("bid", amount=Decimal("1000"))).template_id == "band"
    assert evaluate(rules, EntityParams("bid", amount=Decimal("5000"))).template_id == "band"
    assert evaluate(rules, EntityParams("bid", amount=Decimal("5001"))).template_id == "default"


def test_missing_amount_never_matches_amount_conditions():
    rules = [rule(10, {"amount_max": 5000}, tid="band"), CATCH_ALL]
    assert evaluate(rules, EntityParams("bid")).template_id == "default"


def test_risk_tier_and_product_lists():
    rules = [rule(10, {"risk_tiers": ["A", "B"], "product_types": ["home_loan"]},
                  tid="prime"), CATCH_ALL]
    ok = EntityParams("bid", risk_tier="A", product_type="home_loan")
    bad_tier = EntityParams("bid", risk_tier="D", product_type="home_loan")
    assert evaluate(rules, ok).template_id == "prime"
    assert evaluate(rules, bad_tier).template_id == "default"


def test_entity_type_isolation():
    rules = [rule(10, {}, tid="bids_only", et="bid"),
             rule(9999, {}, tid="mandates", et="investment_mandate")]
    p = EntityParams("investment_mandate")
    assert evaluate(rules, p).template_id == "mandates"


def test_no_catch_all_raises_loudly():
    rules = [rule(10, {"amount_max": 100}, tid="tiny")]
    with pytest.raises(NoMatchingRule):
        evaluate(rules, EntityParams("bid", amount=Decimal("5000")))


def test_simulate_equals_live_property():
    rules = [
        rule(5, {"amount_max": 1_000_000}, tid="a"),
        rule(6, {"amount_max": 1_000_000}, tid="b"),
        CATCH_ALL,
    ]
    p = EntityParams("bid", amount=Decimal("999999"))
    assert evaluate(rules, p).id == "r5"
