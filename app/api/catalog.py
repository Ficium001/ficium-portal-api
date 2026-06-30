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
    """
    Return institution audit events ordered newest-first.

    Column aliases ensure the frontend AuditEvent type is satisfied:
      occurred_at  → created_at   (frontend reads e.created_at)
      action       → event_label  (frontend reads e.event_label)

    state_before / state_after are included for the row detail panel.
    """
    rows = conn.execute(
        text("""
            SELECT
                id,
                actor_id,
                actor_role,
                actor_ip,
                action_category,
                action           AS event_label,
                resource_type,
                resource_label,
                resource_id,
                state_before,
                state_after,
                outcome,
                outcome_note,
                occurred_at      AS created_at
            FROM audit.event
            ORDER BY occurred_at DESC
            LIMIT :lim
        """),
        {"lim": limit},
    ).fetchall()

    result = []
    for r in rows:
        row = dict(r._mapping)
        # Serialise UUIDs and timestamps to strings
        if row.get("id"):
            row["id"] = str(row["id"])
        if row.get("actor_id"):
            row["actor_id"] = str(row["actor_id"])
        if row.get("created_at"):
            row["created_at"] = row["created_at"].isoformat()
        # state_before / state_after are already dicts from JSONB; leave as-is
        result.append(row)
    return result


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
