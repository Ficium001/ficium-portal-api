# =============================================================================
# ficium-portal-api — Catalog & ops router
# Replaces: useWebhooks, useProducts, useAuditEvents
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import tenant_conn

router = APIRouter(tags=["catalog"])


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/webhooks")
async def list_webhooks(conn: Session = Depends(tenant_conn)) -> list[dict]:
    result = conn.execute(
        text("""
            SELECT * FROM institution.institution_webhooks
            ORDER BY created_at DESC
        """)
    )
    return _rows(result)


@router.get("/products")
async def list_products(conn: Session = Depends(tenant_conn)) -> list[dict]:
    """Active products with family label, rate config and SLA defaults."""
    result = conn.execute(
        text("""
            SELECT
                p.*,
                pf.label AS family_label,
                to_jsonb(prc.*) AS rate_config,
                to_jsonb(psd.*) AS sla_defaults
            FROM institution.products p
            LEFT JOIN institution.product_families     pf  ON pf.id  = p.family_id
            LEFT JOIN institution.product_rate_config  prc ON prc.product_id = p.id
            LEFT JOIN institution.product_sla_defaults psd ON psd.product_id = p.id
            WHERE p.active = true
            ORDER BY p.sort_order
        """)
    )
    return _rows(result)


@router.get("/audit")
async def list_audit_events(
    limit: int = Query(default=50, le=500),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    result = conn.execute(
        text("""
            SELECT * FROM institution.audit_events
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"lim": limit},
    )
    return _rows(result)
