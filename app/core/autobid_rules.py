# =============================================================================
# ficium-portal-api — Auto-Bid rule validation + evaluation (inst:autobid)
#
# Rules are declarative JSON condition trees over a CLOSED vocabulary of
# fields and operators — never user-supplied code. This module is the single
# authority for:
#
#   validate_predicate / validate_action / validate_guardrails
#       Structural + vocabulary validation at write time (422 on violation).
#
#   matches(predicate, facts)
#       Pure evaluator. The SAME function will run in the autobid worker
#       against loan-request facts, so approval-time validation and run-time
#       evaluation can never drift.
#
# Determinism contract (see spec §2.3): one rule fires per (institution,
# request) — lowest priority number wins, ties broken by oldest approved_at.
# select_winning_rule() implements that contract and is unit-tested here so
# the worker inherits it rather than re-implementing it.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Closed vocabulary
# ---------------------------------------------------------------------------

# field → allowed python types of the fact value
FIELDS: dict[str, tuple[type, ...]] = {
    "rating_band": (str,),          # 'AAA'..'D'
    "amount_mur": (int, float),
    "tenor_months": (int,),
    "product": (str,),              # e.g. 'personal_unsecured'
    "secured": (bool,),
    "region": (str,),
    "dti_band": (str,),             # 'low' | 'medium' | 'high' (if disclosed)
}

RATING_BANDS = ("AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D")

COMPARISON_OPS = frozenset({"eq", "in", "between", "lte", "gte"})
LOGICAL_KEYS = frozenset({"all", "any", "not"})

LANES = frozenset({"soft_pull", "hard_pull"})

MAX_TREE_DEPTH = 6
MAX_CONDITIONS = 40


class RuleValidationError(ValueError):
    """Predicate/action/guardrails failed closed-vocabulary validation."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_leaf(node: dict[str, Any], path: str) -> None:
    field = node.get("field")
    op = node.get("op")
    value = node.get("value")

    if field not in FIELDS:
        raise RuleValidationError(f"{path}: unknown field {field!r}")
    if op not in COMPARISON_OPS:
        raise RuleValidationError(f"{path}: unknown operator {op!r}")
    if set(node.keys()) - {"field", "op", "value"}:
        raise RuleValidationError(f"{path}: unexpected keys in condition")

    types = FIELDS[field]

    if op == "eq":
        if not isinstance(value, types):
            raise RuleValidationError(f"{path}: eq value has wrong type for {field}")
    elif op == "in":
        if not isinstance(value, list) or not value:
            raise RuleValidationError(f"{path}: 'in' requires a non-empty list")
        if not all(isinstance(v, types) for v in value):
            raise RuleValidationError(f"{path}: 'in' list has wrong element type for {field}")
    elif op == "between":
        if field == "rating_band" or types == (str,) or types == (bool,):
            raise RuleValidationError(f"{path}: 'between' not valid for {field}")
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
            or value[0] > value[1]
        ):
            raise RuleValidationError(f"{path}: 'between' requires [lo, hi] with lo <= hi")
    else:  # lte / gte
        if types == (bool,) or (types == (str,) and field != "rating_band"):
            raise RuleValidationError(f"{path}: {op} not valid for {field}")
        if field == "rating_band":
            if value not in RATING_BANDS:
                raise RuleValidationError(f"{path}: unknown rating band {value!r}")
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            raise RuleValidationError(f"{path}: {op} requires a numeric value")

    if field == "rating_band" and op in ("eq", "in"):
        vals = value if isinstance(value, list) else [value]
        bad = [v for v in vals if v not in RATING_BANDS]
        if bad:
            raise RuleValidationError(f"{path}: unknown rating bands {bad}")


def _walk(node: Any, path: str, depth: int, counter: list[int]) -> None:
    if depth > MAX_TREE_DEPTH:
        raise RuleValidationError(f"{path}: tree exceeds max depth {MAX_TREE_DEPTH}")
    if not isinstance(node, dict):
        raise RuleValidationError(f"{path}: node must be an object")

    logical = set(node.keys()) & LOGICAL_KEYS
    if logical:
        if len(node.keys()) != 1:
            raise RuleValidationError(f"{path}: logical node must have exactly one key")
        key = logical.pop()
        child = node[key]
        if key == "not":
            _walk(child, f"{path}.not", depth + 1, counter)
        else:
            if not isinstance(child, list) or not child:
                raise RuleValidationError(f"{path}.{key}: requires a non-empty list")
            for i, sub in enumerate(child):
                _walk(sub, f"{path}.{key}[{i}]", depth + 1, counter)
        return

    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise RuleValidationError(f"predicate exceeds {MAX_CONDITIONS} conditions")
    _validate_leaf(node, path)


def validate_predicate(predicate: Any) -> None:
    """Raise RuleValidationError unless the predicate is a valid condition tree."""
    _walk(predicate, "$", 0, [0])


def validate_action(action: Any) -> None:
    if not isinstance(action, dict):
        raise RuleValidationError("action must be an object")
    required = {"apr_pct", "lane", "bid_validity_hours"}
    missing = required - set(action.keys())
    if missing:
        raise RuleValidationError(f"action missing keys: {sorted(missing)}")
    allowed = required | {"fee_schedule"}
    extra = set(action.keys()) - allowed
    if extra:
        raise RuleValidationError(f"action has unexpected keys: {sorted(extra)}")

    apr = action["apr_pct"]
    if not isinstance(apr, (int, float)) or isinstance(apr, bool) or not 0 < apr <= 100:
        raise RuleValidationError("action.apr_pct must be a number in (0, 100]")
    if action["lane"] not in LANES:
        raise RuleValidationError(f"action.lane must be one of {sorted(LANES)}")
    hours = action["bid_validity_hours"]
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 168:
        raise RuleValidationError("action.bid_validity_hours must be an int in [1, 168]")
    if "fee_schedule" in action and not isinstance(action["fee_schedule"], str):
        raise RuleValidationError("action.fee_schedule must be a string reference")


def validate_guardrails(guardrails: Any) -> None:
    if not isinstance(guardrails, dict):
        raise RuleValidationError("guardrails must be an object")
    required = {"max_bids_per_day", "max_principal_per_day_mur"}
    missing = required - set(guardrails.keys())
    if missing:
        raise RuleValidationError(f"guardrails missing keys: {sorted(missing)}")
    allowed = required | {"min_seconds_between_bids", "expires_at"}
    extra = set(guardrails.keys()) - allowed
    if extra:
        raise RuleValidationError(f"guardrails has unexpected keys: {sorted(extra)}")

    mbd = guardrails["max_bids_per_day"]
    if not isinstance(mbd, int) or isinstance(mbd, bool) or not 1 <= mbd <= 10000:
        raise RuleValidationError("guardrails.max_bids_per_day must be an int in [1, 10000]")
    mpd = guardrails["max_principal_per_day_mur"]
    if not isinstance(mpd, (int, float)) or isinstance(mpd, bool) or mpd <= 0:
        raise RuleValidationError("guardrails.max_principal_per_day_mur must be > 0")
    if "min_seconds_between_bids" in guardrails:
        msb = guardrails["min_seconds_between_bids"]
        if not isinstance(msb, int) or isinstance(msb, bool) or not 0 <= msb <= 86400:
            raise RuleValidationError(
                "guardrails.min_seconds_between_bids must be an int in [0, 86400]"
            )
    if "expires_at" in guardrails and not isinstance(guardrails["expires_at"], str):
        raise RuleValidationError("guardrails.expires_at must be an ISO-8601 string")


# ---------------------------------------------------------------------------
# Evaluation (pure — shared with the worker)
# ---------------------------------------------------------------------------

def _rating_rank(band: str) -> int:
    """Lower rank = better rating. AAA=0 ... D=9."""
    return RATING_BANDS.index(band)


def _eval_leaf(node: dict[str, Any], facts: dict[str, Any]) -> bool:
    field, op, value = node["field"], node["op"], node["value"]
    if field not in facts or facts[field] is None:
        return False  # missing fact never matches — fail closed
    actual = facts[field]

    if field == "rating_band" and op in ("lte", "gte"):
        # Semantic ordering: 'gte AAA-side' means AT LEAST this creditworthy.
        # gte X  → rating is X or better (rank <= rank(X))
        # lte X  → rating is X or worse  (rank >= rank(X))
        if actual not in RATING_BANDS:
            return False
        if op == "gte":
            return _rating_rank(actual) <= _rating_rank(value)
        return _rating_rank(actual) >= _rating_rank(value)

    if op == "eq":
        return bool(actual == value)
    if op == "in":
        return actual in value
    if op == "between":
        return bool(value[0] <= actual <= value[1])
    if op == "lte":
        return bool(actual <= value)
    if op == "gte":
        return bool(actual >= value)
    return False


def matches(predicate: dict[str, Any], facts: dict[str, Any]) -> bool:
    """Evaluate a validated predicate tree against request facts. Pure; fails closed."""
    if "all" in predicate:
        return all(matches(sub, facts) for sub in predicate["all"])
    if "any" in predicate:
        return any(matches(sub, facts) for sub in predicate["any"])
    if "not" in predicate:
        return not matches(predicate["not"], facts)
    return _eval_leaf(predicate, facts)


# ---------------------------------------------------------------------------
# Deterministic winner selection (spec §2.3 contract)
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
    """
    Deterministic: among matching candidates, lowest priority number wins;
    ties broken by oldest approved_at, then rule_id for total ordering.
    """
    matching = [c for c in candidates if matches(c.predicate, facts)]
    if not matching:
        return None
    return min(matching, key=lambda c: (c.priority, c.approved_at, c.rule_id))
