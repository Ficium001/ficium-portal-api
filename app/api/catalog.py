# =============================================================================
# ficium-portal-api — Catalog & ops router (v2 schema)
#
# Products:  catalog.product + catalog.product_family (was institution.products)
# Webhooks:  institution.webhook (was institution.institution_webhooks)
# Audit:     audit.event (was institution.audit_events)
# KYB docs:  institution.kyb_document (new)
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import tenant_conn

router = APIRouter(tags=["catalog"])


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


# ── Products ──────────────────────────────────────────────────────────────────

@router.get("/products")
async def list_products(conn: Session = Depends(tenant_conn)) -> list[dict]:
    """
    Active products with family, rate model and SLA defaults.
    Reads catalog.product (v2) — global reference data, no tenant scoping.
    """
    return _rows(conn.execute(text("""
        SELECT
            p.id,
            p.family_id,
            p.code,
            p.label,
            p.description,
            p.currency,
            p.min_amount,
            p.max_amount,
            p.min_term_months,
            p.max_term_months,
            p.active,
            p.sort_order,
            p.metadata,
            pf.code                 AS family_code,
            pf.label                AS family_label,
            pf.icon                 AS family_icon,
            to_jsonb(prm.*)         AS rate_model,
            to_jsonb(ps.*)          AS sla_defaults
        FROM catalog.product             p
        JOIN catalog.product_family      pf  ON pf.id = p.family_id
        LEFT JOIN catalog.product_rate_model prm ON prm.product_id = p.id
        LEFT JOIN catalog.product_sla    ps  ON ps.product_id  = p.id
        WHERE p.active = true
        ORDER BY pf.sort_order, p.sort_order
    """)))


@router.get("/products/{product_id}/parameters")
async def get_product_parameters(
    product_id: str,
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Dynamic parameters for a product (drives request form)."""
    return _rows(conn.execute(
        text("""
            SELECT * FROM catalog.product_parameter
            WHERE product_id = :pid
            ORDER BY sort_order
        """),
        {"pid": product_id},
    ))


@router.get("/products/{product_id}/eligibility")
async def get_product_eligibility(
    product_id: str,
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Eligibility rules for a product."""
    return _rows(conn.execute(
        text("""
            SELECT * FROM catalog.product_eligibility
            WHERE product_id = :pid AND active = true
        """),
        {"pid": product_id},
    ))


# ── Institution product config (per-tenant overrides) ─────────────────────────

@router.get("/product-config")
async def list_product_config(conn: Session = Depends(tenant_conn)) -> list[dict]:
    """Per-institution product overrides (rates, SLA, eligibility conditions)."""
    return _rows(conn.execute(text("""
        SELECT
            pc.*,
            p.code      AS product_code,
            p.label     AS product_label,
            pf.label    AS family_label
        FROM institution.product_config pc
        JOIN catalog.product       p  ON p.id  = pc.product_id
        JOIN catalog.product_family pf ON pf.id = p.family_id
        ORDER BY pf.sort_order, p.sort_order
    """)))


# ── Webhooks ──────────────────────────────────────────────────────────────────

@router.get("/webhooks")
async def list_webhooks(conn: Session = Depends(tenant_conn)) -> list[dict]:
    """Institution webhooks (v2 table name: institution.webhook)."""
    return _rows(conn.execute(text("""
        SELECT
            id, institution_id, label, endpoint_url,
            event_types, active, retry_max, timeout_ms,
            last_fired_at, last_status, failure_count,
            created_at, updated_at
            -- signing_secret intentionally excluded
        FROM institution.webhook
        ORDER BY created_at DESC
    """)))


@router.get("/webhooks/{webhook_id}/deliveries")
async def list_webhook_deliveries(
    webhook_id: str,
    limit: int = Query(default=50, le=200),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Delivery log for a webhook (new in v2)."""
    return _rows(conn.execute(
        text("""
            SELECT
                id, event_type, status, attempts,
                response_status, next_retry_at, delivered_at, created_at
            FROM institution.webhook_delivery
            WHERE webhook_id = :wid
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"wid": webhook_id, "lim": limit},
    ))


# ── KYB Documents (new in v2) ─────────────────────────────────────────────────

@router.get("/kyb-documents")
async def list_kyb_documents(conn: Session = Depends(tenant_conn)) -> list[dict]:
    """KYB document submissions for the institution."""
    return _rows(conn.execute(text("""
        SELECT
            id, doc_type, label, mime_type, file_size_bytes,
            status, rejection_reason, reviewed_at, expires_at,
            uploaded_by, created_at
            -- storage_path intentionally excluded (pre-signed URLs via separate endpoint)
        FROM institution.kyb_document
        ORDER BY created_at DESC
    """)))


# ── Audit ─────────────────────────────────────────────────────────────────────

@router.get("/audit")
async def list_audit_events(
    limit: int = Query(default=50, le=500),
    action: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """
    Institution audit events from audit.event (v2 unified log).
    Falls back to institution.audit_events if audit.event is empty.
    """
    sql = """
        SELECT
            id, occurred_at, actor_id, actor_type,
            actor_email, actor_role, actor_ip,
            action, resource_type, resource_id, resource_label,
            outcome, outcome_note, metadata
        FROM audit.event
        WHERE institution_id IS NOT NULL
    """
    params: dict = {"lim": limit}
    if action:
        sql += " AND action = :action"
        params["action"] = action
    sql += " ORDER BY occurred_at DESC LIMIT :lim"

    rows = conn.execute(text(sql), params).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Modules ───────────────────────────────────────────────────────────────────

@router.get("/modules")
async def list_modules(
    side: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Platform modules from catalog.module (new in v2 — was hardcoded in TS)."""
    sql = "SELECT * FROM catalog.module WHERE active = true"
    params: dict = {}
    if side:
        sql += " AND side = :side"
        params["side"] = side
    sql += " ORDER BY sort_order"
    return _rows(conn.execute(text(sql), params))
