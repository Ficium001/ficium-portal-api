# =============================================================================
# ficium-portal-api — Marketplace router
# =============================================================================
#
# GET /marketplace/requests  — all open client requests (service_session,
#                              bypasses cross-schema RLS; reads public.requests
#                              directly and LEFT JOINs client_financial_snapshot)
#
# GET /marketplace/my-bids   — this institution's bids (tenant_conn + RLS)
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import service_session
from ..deps import tenant_conn

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


_REQUESTS_SQL = """
    SELECT
        r.id,
        r.product_type,
        r.status,
        r.amount::NUMERIC                                               AS amount,
        'MUR'                                                           AS currency,
        r.preferred_term_months                                         AS term_months,
        r.purpose,
        COALESCE(r.decision_deadline,
                 now() + interval '48 hours')                          AS bid_window_closes_at,
        r.created_at,
        LEFT(r.client_id::TEXT, 8)                                     AS client_ref,
        'individual'                                                    AS client_type,
        s.monthly_income                                               AS client_monthly_income,
        s.total_net_worth                                              AS client_net_worth,
        s.health_score                                                 AS client_health_score,
        s.employment_status                                            AS client_employment_status
    FROM  public.requests               r
    LEFT JOIN public.client_financial_snapshot s ON s.client_id = r.client_id
    WHERE r.status = 'open'
"""


@router.get("/requests")
async def list_requests(
    product_type: str | None = Query(default=None),
) -> list[dict]:
    """
    Open marketplace requests visible to all approved institutions.
    Uses service_session() — no JWT needed — because the marketplace is
    cross-schema (public.requests) and all institutions see the same pool.
    Client PII is never exposed: only anonymised financial signals.
    """
    with service_session() as conn:
        if product_type:
            result = conn.execute(
                text(_REQUESTS_SQL + " AND r.product_type = :pt ORDER BY r.created_at DESC"),
                {"pt": product_type},
            )
        else:
            result = conn.execute(
                text(_REQUESTS_SQL + " ORDER BY r.created_at DESC")
            )
        return _rows(result)


@router.get("/my-bids")
async def list_my_bids(
    status: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """This institution's bids (RLS-scoped via tenant JWT)."""
    if status:
        result = conn.execute(
            text("""
                SELECT * FROM institution.institution_bids
                WHERE status = :st
                ORDER BY submitted_at DESC
            """),
            {"st": status},
        )
    else:
        result = conn.execute(
            text("""
                SELECT * FROM institution.institution_bids
                ORDER BY submitted_at DESC
            """)
        )
    return _rows(result)
