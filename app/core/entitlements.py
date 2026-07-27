# =============================================================================
# ficium-portal-api — Module entitlement enforcement + usage metering
#
# Commercial layer for per-module packaging (AUTOBID / SERVICING / RATING_API).
#
#   require_module("AUTOBID")   FastAPI dependency factory. Resolves the
#                               caller's institution_id from unified claims
#                               (JWT or API key), checks an active entitlement,
#                               403s with a machine-readable code otherwise.
#
#   record_usage(...)           Append one row to the metering ledger.
#                               Idempotency-keyed: safe under retries and
#                               at-least-once event delivery.
#
# Enforcement lives HERE, at the API layer — deliberately NOT in RLS.
# RLS remains pure tenant isolation; entitlement is commercial state.
# (See db/009_entitlements.sql header and the module architecture spec.)
#
# Caching: entitlement lookups are cached in-process for ENTITLEMENT_CACHE_TTL
# seconds, so suspension propagates platform-wide within that window without
# adding a DB round-trip to every request.
# =============================================================================

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

import structlog
from fastapi import Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import api_or_jwt_claims
from .db import service_session

log = structlog.get_logger(__name__)

ENTITLEMENT_CACHE_TTL = 60.0  # seconds

_ALLOWED_STATUSES = frozenset({"active", "trial"})


@dataclass(frozen=True)
class Entitlement:
    """Resolved entitlement snapshot for (institution, module)."""

    institution_id: str
    module_code: str
    status: str
    plan_tier: str
    valid_from: datetime
    valid_to: datetime | None
    limits: dict[str, Any]


class EntitlementError(Exception):
    """Entitlement missing, suspended, expired or not yet valid."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


def evaluate(row: Entitlement | None, now: datetime) -> Entitlement:
    """
    Pure decision function: given the stored entitlement row (or None) and the
    current time, return the entitlement or raise EntitlementError.

    Kept side-effect free so guardrail behaviour is unit-testable without a DB.
    """
    if row is None:
        raise EntitlementError(
            "module_not_licensed", "Module is not licensed for this institution."
        )
    if row.status not in _ALLOWED_STATUSES:
        raise EntitlementError("module_suspended", f"Module entitlement is {row.status}.")
    if row.valid_from > now:
        raise EntitlementError("module_not_yet_valid", "Module entitlement is not yet valid.")
    if row.valid_to is not None and row.valid_to <= now:
        raise EntitlementError("module_expired", "Module entitlement has expired.")
    return row


# ---------------------------------------------------------------------------
# In-process TTL cache
# ---------------------------------------------------------------------------

_cache: dict[tuple[str, str], tuple[float, Entitlement | None]] = {}
_cache_lock = Lock()


def _cache_get(key: tuple[str, str]) -> tuple[bool, Entitlement | None]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return False, None
        expires, value = hit
        if expires < time.monotonic():
            del _cache[key]
            return False, None
        return True, value


def _cache_put(key: tuple[str, str], value: Entitlement | None) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic() + ENTITLEMENT_CACHE_TTL, value)


def invalidate_cache(institution_id: str | None = None) -> None:
    """Drop cached entitlements (all, or one institution's). Used by admin flows/tests."""
    with _cache_lock:
        if institution_id is None:
            _cache.clear()
        else:
            for key in [k for k in _cache if k[0] == institution_id]:
                del _cache[key]


# ---------------------------------------------------------------------------
# Lookup + dependency factory
# ---------------------------------------------------------------------------

_LOOKUP_SQL = text(
    """
    SELECT institution_id::text, module_code, status, plan_tier,
           valid_from, valid_to, limits
    FROM entitlements.institution_entitlement
    WHERE institution_id = :institution_id AND module_code = :module_code
    """
)


def _load_entitlement(conn: Session, institution_id: str, module_code: str) -> Entitlement | None:
    row = conn.execute(
        _LOOKUP_SQL, {"institution_id": institution_id, "module_code": module_code}
    ).mappings().first()
    if row is None:
        return None
    return Entitlement(
        institution_id=row["institution_id"],
        module_code=row["module_code"],
        status=row["status"],
        plan_tier=row["plan_tier"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        limits=dict(row["limits"] or {}),
    )


def resolve_entitlement(institution_id: str, module_code: str) -> Entitlement:
    """
    Resolve and validate the entitlement for (institution, module),
    via the TTL cache. Raises EntitlementError when not entitled.
    """
    key = (institution_id, module_code)
    cached, row = _cache_get(key)
    if not cached:
        with service_session() as conn:
            row = _load_entitlement(conn, institution_id, module_code)
        _cache_put(key, row)
    return evaluate(row, datetime.now(UTC))


def require_module(module_code: str):
    """
    Dependency factory — gate a router (or endpoint) on an active module
    entitlement for the caller's institution. Works for both auth paths
    (portal JWT and machine API key) via api_or_jwt_claims.

    Usage:
        router = APIRouter(
            prefix="/v1/autobid",
            dependencies=[Depends(require_module("AUTOBID"))],
        )
    """

    def _check(claims: dict[str, Any] = Depends(api_or_jwt_claims)) -> dict[str, Any]:
        institution_id = claims.get("institution_id")
        if not institution_id:
            raise HTTPException(status_code=403, detail="No institution context in credentials.")
        try:
            ent = resolve_entitlement(str(institution_id), module_code)
        except EntitlementError as e:
            log.info(
                "entitlement_denied",
                institution_id=str(institution_id),
                module=module_code,
                code=e.code,
            )
            raise HTTPException(
                status_code=403,
                detail={"code": e.code, "module": module_code, "message": str(e)},
            ) from e
        claims["entitlement"] = ent
        return claims

    return _check


# ---------------------------------------------------------------------------
# Usage metering
# ---------------------------------------------------------------------------

_USAGE_SQL = text(
    """
    INSERT INTO entitlements.usage_event
        (institution_id, module_code, unit, quantity, idempotency_key, metadata)
    VALUES
        (:institution_id, :module_code, :unit, :quantity, :idempotency_key,
         CAST(:metadata AS jsonb))
    ON CONFLICT (idempotency_key, occurred_at) DO NOTHING
    """
)


def record_usage(
    conn: Session,
    *,
    institution_id: str,
    module_code: str,
    unit: str,
    idempotency_key: str,
    quantity: float = 1,
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Append a metering event. Callers pass the session that owns the business
    transaction so the meter commits atomically with the work it measures
    (e.g. bid INSERT + usage row in one transaction — never one without the other).

    NOTE: the ledger UNIQUE constraint is (idempotency_key, occurred_at) because
    occurred_at is the partition key; callers MUST therefore use idempotency
    keys that are deterministic per business event (e.g. "bid:{rule_version_id}:
    {loan_request_id}") so retried transactions reproduce the same key.
    """
    import json

    conn.execute(
        _USAGE_SQL,
        {
            "institution_id": institution_id,
            "module_code": module_code,
            "unit": unit,
            "quantity": quantity,
            "idempotency_key": idempotency_key,
            "metadata": json.dumps(metadata or {}),
        },
    )
