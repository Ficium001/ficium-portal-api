"""Delegation-of-authority evaluator.

Pure, deterministic, no I/O. Used by BOTH /doa-rules/simulate and live
routing in service.route_entity — what you simulate is what will happen.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EntityParams:
    entity_type: str
    amount: Decimal | None = None
    currency: str | None = None
    risk_tier: str | None = None
    product_type: str | None = None
    secured: bool | None = None
    tenor_months: int | None = None


@dataclass(frozen=True)
class DoaRule:
    id: str
    entity_type: str
    priority: int
    conditions: dict[str, Any]
    template_id: str


class NoMatchingRule(Exception):
    """Should be impossible in production: a catch-all rule is enforced
    at institution setup. Raised so tests catch misconfiguration loudly."""


def _matches(cond: dict[str, Any], p: EntityParams) -> bool:
    if "amount_min" in cond:
        if p.amount is None or p.amount < Decimal(str(cond["amount_min"])):
            return False
    if "amount_max" in cond:
        if p.amount is None or p.amount > Decimal(str(cond["amount_max"])):
            return False
    if "currency" in cond and cond["currency"] is not None:
        if p.currency != cond["currency"]:
            return False
    if "risk_tiers" in cond and cond["risk_tiers"]:
        if p.risk_tier not in cond["risk_tiers"]:
            return False
    if "product_types" in cond and cond["product_types"]:
        if p.product_type not in cond["product_types"]:
            return False
    if "secured" in cond and cond["secured"] is not None:
        if p.secured is None or p.secured != bool(cond["secured"]):
            return False
    if "tenor_months_max" in cond:
        if p.tenor_months is None or p.tenor_months > int(cond["tenor_months_max"]):
            return False
    return True


def evaluate(rules: list[DoaRule], params: EntityParams) -> DoaRule:
    """First matching rule by ascending priority wins."""
    applicable = sorted(
        (r for r in rules if r.entity_type == params.entity_type),
        key=lambda r: r.priority,
    )
    for rule in applicable:
        if _matches(rule.conditions, params):
            return rule
    raise NoMatchingRule(
        f"No DoA rule matched entity_type={params.entity_type}; "
        "catch-all rule missing — institution misconfigured."
    )
