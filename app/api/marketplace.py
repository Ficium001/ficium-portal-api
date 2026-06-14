# =============================================================================
# ficium-portal-api — Marketplace router
# Replaces: useMarketplace, useMyBids
# Reads marketplace_requests / my_bids views (RLS-scoped).
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import tenant_conn

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/requests")
async def list_requests(
    product_type: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Open marketplace requests, optionally filtered by product type."""
    if product_type:
        result = conn.execute(
            text("""
                SELECT * FROM institution.marketplace_requests
                WHERE product_type = :pt
                ORDER BY created_at DESC
            """),
            {"pt": product_type},
        )
    else:
        result = conn.execute(
            text("""
                SELECT * FROM institution.marketplace_requests
                ORDER BY created_at DESC
            """)
        )
    return _rows(result)


@router.get("/my-bids")
async def list_my_bids(
    status: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """The caller institution's bids, optionally filtered by status."""
    if status:
        result = conn.execute(
            text("""
                SELECT * FROM institution.my_bids
                WHERE status = :st
                ORDER BY submitted_at DESC
            """),
            {"st": status},
        )
    else:
        result = conn.execute(
            text("""
                SELECT * FROM institution.my_bids
                ORDER BY submitted_at DESC
            """)
        )
    return _rows(result)
