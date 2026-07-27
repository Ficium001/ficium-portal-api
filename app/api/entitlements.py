# =============================================================================
# ficium-portal-api — Entitlements router (self-service visibility)
#
#   GET /v1/entitlements/me       modules + entitlement status for the caller's
#                                 institution (drives portal module gating UX)
#   GET /v1/entitlements/usage    current-period usage totals per module
#
# Read-only by design: entitlements are granted/suspended by platform
# operations (service role), never by institutions themselves. RLS restricts
# both tables to SELECT for tenants; these endpoints use the tenant session
# so isolation is enforced by the database, not by this code.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import tenant_conn

router = APIRouter(prefix="/v1/entitlements", tags=["entitlements"])


class EntitlementOut(BaseModel):
    module_code: str
    module_name: str
    billing_model: str
    metered_unit: str | None
    entitled: bool
    status: str | None = None
    plan_tier: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    limits: dict[str, Any] = {}


class UsageLineOut(BaseModel):
    module_code: str
    unit: str
    quantity: float


@router.get("/me", response_model=list[EntitlementOut])
def my_entitlements(conn: Session = Depends(tenant_conn)) -> list[EntitlementOut]:
    """
    Full module catalog joined with the caller's entitlements.
    Unlicensed modules are returned with entitled=false so the portal can
    render locked module cards (discovery/upsell surface).
    """
    rows = conn.execute(
        text(
            """
            SELECT m.module_code, m.name AS module_name, m.billing_model, m.metered_unit,
                   ie.status, ie.plan_tier, ie.valid_from, ie.valid_to, ie.limits
            FROM entitlements.module m
            LEFT JOIN entitlements.institution_entitlement ie
                   ON ie.module_code = m.module_code
            WHERE m.status = 'active'
            ORDER BY m.module_code
            """
        )
    ).mappings().all()

    out: list[EntitlementOut] = []
    for r in rows:
        entitled = r["status"] in ("active", "trial")
        out.append(
            EntitlementOut(
                module_code=r["module_code"],
                module_name=r["module_name"],
                billing_model=r["billing_model"],
                metered_unit=r["metered_unit"],
                entitled=bool(entitled),
                status=r["status"],
                plan_tier=r["plan_tier"],
                valid_from=r["valid_from"],
                valid_to=r["valid_to"],
                limits=dict(r["limits"] or {}) if r["limits"] is not None else {},
            )
        )
    return out


@router.get("/usage", response_model=list[UsageLineOut])
def my_usage(
    conn: Session = Depends(tenant_conn),
    days: int = Query(default=30, ge=1, le=366),
) -> list[UsageLineOut]:
    """Usage totals per (module, unit) over the trailing window. Tenant-scoped by RLS."""
    rows = conn.execute(
        text(
            """
            SELECT module_code, unit, COALESCE(SUM(quantity), 0) AS quantity
            FROM entitlements.usage_event
            WHERE occurred_at >= now() - make_interval(days => :days)
            GROUP BY module_code, unit
            ORDER BY module_code, unit
            """
        ),
        {"days": days},
    ).mappings().all()
    return [
        UsageLineOut(module_code=r["module_code"], unit=r["unit"], quantity=float(r["quantity"]))
        for r in rows
    ]
