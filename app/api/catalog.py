# =============================================================================
# ficium-portal-api — Catalog & ops router
# Replaces: useWebhooks, useProducts, useAuditEvents, SlaTab upsert
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import current_claims, tenant_conn

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
            ORDER BY occurred_at DESC
            LIMIT :lim
        """),
        {"lim": limit},
    )
    return _rows(result)


@router.post("/sla-config")
async def upsert_sla_config(
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Upsert institution SLA config for a product.
    body: { product_code, bid_window_minutes, auto_withdraw_minutes }
    institution_id is taken from JWT claims — never from the body.
    """
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    missing = {"product_code", "bid_window_minutes", "auto_withdraw_minutes"} - set(body)
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {sorted(missing)}")

    conn.execute(
        text("""
            INSERT INTO institution.institution_sla_config
                (institution_id, product_code, bid_window_minutes, auto_withdraw_minutes)
            VALUES
                (:iid, :code, :bid, :auto)
            ON CONFLICT (institution_id, product_code)
            DO UPDATE SET
                bid_window_minutes    = EXCLUDED.bid_window_minutes,
                auto_withdraw_minutes = EXCLUDED.auto_withdraw_minutes,
                updated_at            = now()
        """),
        {
            "iid":  institution_id,
            "code": body["product_code"],
            "bid":  int(body["bid_window_minutes"]),
            "auto": int(body["auto_withdraw_minutes"]),
        },
    )
    conn.commit()
    return {"ok": True}
