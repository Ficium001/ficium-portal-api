# =============================================================================
# ficium-portal-api — Auto-Bid rule language: validation + evaluation (inst:autobid)
#
# Rules are declarative JSON condition trees over a CLOSED vocabulary of the
# anonymous request facts Ficium already shares with banks (app/core/
# autobid_facts.py builds them; the same object goes to a bank's own decision
# engine and to GET /v1/requests). Never user-supplied code. This module is the
# single authority for:
#
#   validate_predicate / validate_action / validate_guardrails
#       Structural + vocabulary validation at write time (422 on violation).
#   matches(predicate, facts)
#       Pure evaluator, the SAME function the engine runs, so approval-time
#       validation and run-time evaluation can never drift.
#   price(action, facts)
#       The bid a matching rule makes for a request (rate, amount, term).
#   select_winning_rule(candidates, facts)
#       One rule fires per (bank, request): lowest priority number wins, ties by
#       oldest approval, then rule id.
#
# Fail closed, three-valued: a condition on a fact the request does not carry is
# UNKNOWN, not false. UNKNOWN never matches, and stays UNKNOWN under `not`, so
# {"not": {"field": "age", ...}} cannot match a request with no age. A rule only
# fires on facts that are actually there.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeGuard

# ---------------------------------------------------------------------------
# Closed vocabulary: field -> (python types of the value, kind)
#   kind "num"  : eq, in, between, lte, gte
#   kind "str"  : eq, in
#   kind "bool" : eq
#   kind "tier" : eq, in, lte, gte  (A best ... D weakest; "lte B" = A or B)
# ---------------------------------------------------------------------------

PRODUCT_TYPES = (
    "sme_loan",
    "personal_loan",
    "mortgage",
    "fixed_deposit",
    "savings_account",
    "credit_card",
    "business_account",
    "investment_account",
    "leasing",
    "overdraft",
    "business_loan",
    "equities",
    "unit_trust",
    "savings_plan",
    "government_bonds",
    "offshore_investment",
    "mixed_portfolio",
)
RISK_TIERS = ("A", "B", "C", "D")

FIELDS: dict[str, str] = {
    # the request
    "product": "str",
    "amount": "num",
    "term_months": "num",
    "currency": "str",
    # Ficium's assessment
    "risk_tier": "tier",
    "ficium_score": "num",
    # verification
    "kyc_verified": "bool",
    "income_verified": "bool",
    # employment and income
    "employment_status": "str",
    "employment_type": "str",
    "years_employed": "num",
    "gross_monthly_income": "num",
    # affordability and existing debt
    "dsr_current_pct": "num",
    "dsr_post_pct": "num",
    "has_existing_loans": "bool",
    "existing_monthly_repayment": "num",
    "net_worth_band": "str",
    "affordability_score": "num",
    # borrower
    "age": "num",
    # security
    "collateral_type": "str",
    "ltv_pct": "num",
    # saving and investing
    "risk_appetite": "str",
    "investment_horizon": "str",
    "liquidity_pref": "str",
    "target_amount": "num",
    "monthly_contribution": "num",
}

OPS_BY_KIND: dict[str, frozenset[str]] = {
    "num": frozenset({"eq", "in", "between", "lte", "gte"}),
    "str": frozenset({"eq", "in"}),
    "bool": frozenset({"eq"}),
    "tier": frozenset({"eq", "in", "lte", "gte"}),
}
LOGICAL_KEYS = frozenset({"all", "any", "not"})

MAX_TREE_DEPTH = 6
MAX_CONDITIONS = 40
MAX_IN_ITEMS = 50
MAX_STR_LEN = 80


class RuleValidationError(ValueError):
    """Predicate/action/guardrails failed closed-vocabulary validation."""


def _is_num(v: Any) -> TypeGuard[float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_value(kind: str, field: str, v: Any, path: str) -> None:
    if kind == "num":
        if not _is_num(v):
            raise RuleValidationError(f"{path}: {field} needs a number")
    elif kind == "bool":
        if not isinstance(v, bool):
            raise RuleValidationError(f"{path}: {field} needs true or false")
    elif kind == "tier":
        if v not in RISK_TIERS:
            raise RuleValidationError(f"{path}: risk tier must be one of {', '.join(RISK_TIERS)}")
    else:
        if not isinstance(v, str) or not v.strip() or len(v) > MAX_STR_LEN:
            raise RuleValidationError(f"{path}: {field} needs text of 1-{MAX_STR_LEN} characters")
        if field == "product" and v not in PRODUCT_TYPES:
            raise RuleValidationError(f"{path}: unknown product {v!r}")


# ---------------------------------------------------------------------------
# Predicate validation
# ---------------------------------------------------------------------------


def _validate_leaf(node: dict[str, Any], path: str) -> None:
    if set(node.keys()) - {"field", "op", "value"}:
        raise RuleValidationError(f"{path}: unexpected keys in condition")
    field, op, value = node.get("field"), node.get("op"), node.get("value")
    if field not in FIELDS:
        raise RuleValidationError(f"{path}: unknown field {field!r}")
    kind = FIELDS[field]
    if op not in OPS_BY_KIND[kind]:
        raise RuleValidationError(f"{path}: operator {op!r} is not valid for {field}")

    if op == "in":
        if not isinstance(value, list) or not value or len(value) > MAX_IN_ITEMS:
            raise RuleValidationError(f"{path}: 'in' needs a list of 1-{MAX_IN_ITEMS} values")
        for i, v in enumerate(value):
            _check_value(kind, field, v, f"{path}.value[{i}]")
    elif op == "between":
        if not isinstance(value, list) or len(value) != 2 or not all(_is_num(v) for v in value):
            raise RuleValidationError(f"{path}: 'between' needs [low, high]")
        if value[0] > value[1]:
            raise RuleValidationError(f"{path}: 'between' needs low <= high")
    else:
        _check_value(kind, field, value, f"{path}.value")


def _walk(node: Any, path: str, depth: int, counter: list[int]) -> None:
    if depth > MAX_TREE_DEPTH:
        raise RuleValidationError(f"{path}: conditions nested deeper than {MAX_TREE_DEPTH}")
    if not isinstance(node, dict):
        raise RuleValidationError(f"{path}: each condition must be an object")
    logical = set(node.keys()) & LOGICAL_KEYS
    if logical:
        if len(node.keys()) != 1:
            raise RuleValidationError(f"{path}: a group has exactly one of all / any / not")
        key = logical.pop()
        child = node[key]
        if key == "not":
            _walk(child, f"{path}.not", depth + 1, counter)
            return
        if not isinstance(child, list) or not child:
            raise RuleValidationError(f"{path}.{key}: needs a non-empty list")
        for i, sub in enumerate(child):
            _walk(sub, f"{path}.{key}[{i}]", depth + 1, counter)
        return
    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise RuleValidationError(f"more than {MAX_CONDITIONS} conditions")
    _validate_leaf(node, path)


def validate_predicate(predicate: Any) -> None:
    """Raise RuleValidationError unless the predicate is a valid condition tree."""
    _walk(predicate, "$", 0, [0])
    if not _mentions(predicate, "product"):
        # A rule with no product condition would bid on savings and loans alike.
        raise RuleValidationError("a rule must say which product(s) it bids on")


def _mentions(node: dict[str, Any], field: str) -> bool:
    if "all" in node:
        return any(_mentions(s, field) for s in node["all"])
    if "any" in node:
        return all(_mentions(s, field) for s in node["any"])
    if "not" in node:
        return False
    return bool(node.get("field") == field and node.get("op") in ("eq", "in"))


# ---------------------------------------------------------------------------
# Action: the bid a matching rule makes
#   {
#     "rate_pct": 11.9,                       # base rate, % a year
#     "rate_by_tier": {"A": 9.9, "B": 11.9},  # optional; overrides rate_pct per tier
#     "rate_type": "fixed" | "variable",
#     "amount": {"mode": "requested" | "fixed" | "pct_of_requested", "value": n},
#     "term":   {"mode": "requested" | "fixed", "value": n},
#     "rate_valid_days": 1-90,
#     "conditions": "text shown to the borrower" (optional, <= 1000)
#   }
# ---------------------------------------------------------------------------

_ACTION_KEYS = {
    "rate_pct",
    "rate_by_tier",
    "rate_type",
    "amount",
    "term",
    "rate_valid_days",
    "conditions",
}


def _rate_ok(v: Any) -> bool:
    return _is_num(v) and 0 < v <= 100


def validate_action(action: Any) -> None:
    if not isinstance(action, dict):
        raise RuleValidationError("action must be an object")
    extra = set(action) - _ACTION_KEYS
    if extra:
        raise RuleValidationError(f"action has unexpected keys: {sorted(extra)}")
    for k in ("rate_pct", "rate_type", "amount", "term", "rate_valid_days"):
        if k not in action:
            raise RuleValidationError(f"action needs {k}")
    if not _rate_ok(action["rate_pct"]):
        raise RuleValidationError("rate_pct must be a number above 0 and at most 100")
    tiers = action.get("rate_by_tier")
    if tiers is not None:
        if not isinstance(tiers, dict) or not tiers or set(tiers) - set(RISK_TIERS):
            raise RuleValidationError(f"rate_by_tier keys must be {', '.join(RISK_TIERS)}")
        if not all(_rate_ok(v) for v in tiers.values()):
            raise RuleValidationError("every rate_by_tier rate must be above 0 and at most 100")
    if action["rate_type"] not in ("fixed", "variable"):
        raise RuleValidationError("rate_type must be fixed or variable")

    amount = action["amount"]
    if not isinstance(amount, dict) or amount.get("mode") not in (
        "requested",
        "fixed",
        "pct_of_requested",
    ):
        raise RuleValidationError("amount.mode must be requested, fixed or pct_of_requested")
    if amount["mode"] == "fixed" and not (_is_num(amount.get("value")) and amount["value"] > 0):
        raise RuleValidationError("amount.value must be above 0")
    if amount["mode"] == "pct_of_requested" and not (
        _is_num(amount.get("value")) and 0 < amount["value"] <= 100
    ):
        raise RuleValidationError("amount.value must be a percentage above 0 and at most 100")

    term = action["term"]
    if not isinstance(term, dict) or term.get("mode") not in ("requested", "fixed"):
        raise RuleValidationError("term.mode must be requested or fixed")
    if term["mode"] == "fixed" and not (
        isinstance(term.get("value"), int)
        and not isinstance(term["value"], bool)
        and 1 <= term["value"] <= 480
    ):
        raise RuleValidationError("term.value must be whole months from 1 to 480")

    days = action["rate_valid_days"]
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
        raise RuleValidationError("rate_valid_days must be whole days from 1 to 90")
    cond = action.get("conditions")
    if cond is not None and (not isinstance(cond, str) or len(cond) > 1000):
        raise RuleValidationError("conditions must be text of at most 1000 characters")


# ---------------------------------------------------------------------------
# Guardrails (per rule; the bank-wide daily caps live in institution_config)
#   {"max_bids_per_day": int, "max_amount_per_day": number, "expires_at": "ISO date"?}
# ---------------------------------------------------------------------------


def validate_guardrails(guardrails: Any) -> None:
    if not isinstance(guardrails, dict):
        raise RuleValidationError("guardrails must be an object")
    extra = set(guardrails) - {"max_bids_per_day", "max_amount_per_day", "expires_at"}
    if extra:
        raise RuleValidationError(f"guardrails has unexpected keys: {sorted(extra)}")
    mbd = guardrails.get("max_bids_per_day")
    if not isinstance(mbd, int) or isinstance(mbd, bool) or not 1 <= mbd <= 10000:
        raise RuleValidationError("max_bids_per_day must be a whole number from 1 to 10000")
    mad = guardrails.get("max_amount_per_day")
    if not _is_num(mad) or mad <= 0:
        raise RuleValidationError("max_amount_per_day must be above 0")
    exp = guardrails.get("expires_at")
    if exp is not None:
        try:
            datetime.fromisoformat(str(exp))
        except ValueError as e:
            raise RuleValidationError("expires_at must be an ISO date or date-time") from e


# ---------------------------------------------------------------------------
# Evaluation (pure, three-valued: True / False / None = unknown)
# ---------------------------------------------------------------------------


def _tier_rank(t: Any) -> int | None:
    return RISK_TIERS.index(t) if t in RISK_TIERS else None


def _eval_leaf(node: dict[str, Any], facts: dict[str, Any]) -> bool | None:
    field, op, value = node["field"], node["op"], node["value"]
    actual = facts.get(field)
    if actual is None:
        return None
    kind = FIELDS[field]
    if kind == "tier":
        rank = _tier_rank(actual)
        if rank is None:
            return None
        if op == "eq":
            return actual == value
        if op == "in":
            return actual in value
        target = _tier_rank(value)
        assert target is not None
        return rank <= target if op == "lte" else rank >= target
    if kind == "num":
        if not _is_num(actual):
            return None
        if op == "eq":
            return bool(actual == value)
        if op == "in":
            return actual in value
        if op == "between":
            return bool(value[0] <= actual <= value[1])
        if op == "lte":
            return bool(actual <= value)
        return bool(actual >= value)
    if kind == "bool":
        return actual is value if isinstance(actual, bool) else None
    if not isinstance(actual, str):
        return None
    return actual == value if op == "eq" else actual in value


def evaluate(predicate: dict[str, Any], facts: dict[str, Any]) -> bool | None:
    """Kleene logic: unknown stays unknown unless the group is already decided."""
    if "all" in predicate:
        out: bool | None = True
        for sub in predicate["all"]:
            r = evaluate(sub, facts)
            if r is False:
                return False
            if r is None:
                out = None
        return out
    if "any" in predicate:
        any_out: bool | None = False
        for sub in predicate["any"]:
            r = evaluate(sub, facts)
            if r is True:
                return True
            if r is None:
                any_out = None
        return any_out
    if "not" in predicate:
        r = evaluate(predicate["not"], facts)
        return None if r is None else not r
    return _eval_leaf(predicate, facts)


def matches(predicate: dict[str, Any], facts: dict[str, Any]) -> bool:
    """True only when the rule definitely matches the facts that are there."""
    return evaluate(predicate, facts) is True


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PricedBid:
    rate_pct: float
    rate_type: str
    amount: float
    term_months: int
    rate_valid_days: int
    conditions: str | None


def price(action: dict[str, Any], facts: dict[str, Any]) -> PricedBid | None:
    """The bid this action makes for these facts, or None when it cannot be priced
    (e.g. 'requested' term on a request with no term)."""
    rate = float(action["rate_pct"])
    tier = facts.get("risk_tier")
    tiers = action.get("rate_by_tier") or {}
    if tier in tiers:
        rate = float(tiers[tier])

    raw_req = facts.get("amount")
    requested: float | None = float(raw_req) if _is_num(raw_req) else None
    a = action["amount"]
    if a["mode"] == "fixed":
        amount = float(a["value"])
    else:
        if requested is None or requested <= 0:
            return None
        amount = requested * (
            float(a["value"]) / 100 if a["mode"] == "pct_of_requested" else 1.0
        )
    if requested is not None and requested > 0:
        amount = min(amount, requested)  # never offer more than was asked
    amount = round(amount, 2)
    if amount <= 0:
        return None

    t = action["term"]
    if t["mode"] == "fixed":
        term = int(t["value"])
    else:
        req_term = facts.get("term_months")
        if not isinstance(req_term, int) or isinstance(req_term, bool) or req_term < 1:
            return None
        term = req_term
    return PricedBid(
        rate,
        action["rate_type"],
        amount,
        term,
        int(action["rate_valid_days"]),
        action.get("conditions"),
    )


# ---------------------------------------------------------------------------
# Deterministic winner selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRule:
    rule_id: str
    rule_version_id: str
    priority: int
    approved_at: datetime
    predicate: dict[str, Any]


def select_winning_rule(
    candidates: list[CandidateRule], facts: dict[str, Any]
) -> CandidateRule | None:
    """Among matching candidates, lowest priority number wins; ties by oldest
    approved_at, then rule_id for a total order."""
    matching = [c for c in candidates if matches(c.predicate, facts)]
    if not matching:
        return None
    return min(matching, key=lambda c: (c.priority, c.approved_at, c.rule_id))


def vocabulary() -> dict[str, Any]:
    """What the portal's rule builder offers: fields, their operators, enums."""
    return {
        "fields": [
            {"field": f, "kind": k, "ops": sorted(OPS_BY_KIND[k])} for f, k in FIELDS.items()
        ],
        "products": list(PRODUCT_TYPES),
        "risk_tiers": list(RISK_TIERS),
    }
