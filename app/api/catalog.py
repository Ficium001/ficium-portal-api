# =============================================================================
# ficium-portal-api — Catalog & ops router
# Replaces: useWebhooks, useProducts, useAuditEvents
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import tenant_conn

router = APIRouter(tags=["catalog"])


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/webhooks")
async def list_webhooks(conn: Session = Depends(tenant_conn)) -> list[dict]:
    result = conn.execute(
        text("""
            SELECT * FROM institution.webhook
            ORDER BY created_at DESC
        """)
    )
    return _rows(result)


@router.get("/products")
async def list_products(conn: Session = Depends(tenant_conn)) -> list[dict]:
    """Active catalog products with family label, rate model and SLA config."""
    result = conn.execute(
        text("""
            SELECT
                p.*,
                pf.label                AS family_label,
                to_jsonb(prm.*)         AS rate_config,
                to_jsonb(psla.*)        AS sla_defaults
            FROM catalog.product p
            LEFT JOIN catalog.product_family     pf   ON pf.id   = p.family_id
            LEFT JOIN catalog.product_rate_model prm  ON prm.product_id = p.id
            LEFT JOIN catalog.product_sla        psla ON psla.product_id = p.id
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
            SELECT * FROM audit.event
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"lim": limit},
    )
    return _rows(result)
