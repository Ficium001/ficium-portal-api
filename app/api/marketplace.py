# =============================================================================
# ficium-portal-api — Marketplace router (v2 schema)
#
# GET /marketplace/requests  — open requests from marketplace.request
#                              (was public.requests in app DB — now synced here)
# GET /marketplace/my-bids   — from marketplace.my_bids view (security_invoker)
# POST /marketplace/bids     — submit a bid to marketplace.bid
# GET /marketplace/bids/{id} — single bid detail
# =============================================================================

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import AppDatabaseUnavailable, app_service_session, service_session
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


def _row(r) -> dict:
    return dict(r._mapping)


# ── GET /marketplace/requests ─────────────────────────────────────────────────

@router.get("/requests")
async def list_requests(
    product_type: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """
    Open marketplace requests visible to all active institution members.
    Reads marketplace.request (v2) — single DB, real FK to catalog.product.
    Falls back to app_service_session (old cross-DB path) if marketplace.request
    is empty (pre-migration state).
    """
    # Try v2 marketplace schema first
    sql = """
        SELECT
            r.id,
            r.consumer_ref,
            r.product_id,
            p.label                 AS product_label,
            pf.label                AS product_family_label,
            r.country,
            r.currency,
            r.amount,
            r.term_months,
            r.params,
            r.status,
            r.bid_window_opens_at,
            r.bid_window_closes_at,
            r.source,
            r.created_at
        FROM marketplace.request r
        JOIN catalog.product        p  ON p.id  = r.product_id
        JOIN catalog.product_family pf ON pf.id = p.family_id
        WHERE r.status IN ('open', 'bidding')
    """
    params: dict = {}
    if product_type:
        sql += " AND p.code = :pt"
        params["pt"] = product_type
    sql += " ORDER BY r.bid_window_closes_at ASC"

    rows = conn.execute(text(sql), params).fetchall()

    # Pre-migration fallback — marketplace.request empty, read old app DB
    if not rows:
        try:
            with app_service_session() as app_conn:
                _LEGACY_SQL = """
                    SELECT
                        r.id,
                        r.product_type,
                        r.status,
                        r.amount::NUMERIC                                   AS amount,
                        'MUR'                                               AS currency,
                        r.preferred_term_months                             AS term_months,
                        r.purpose,
                        COALESCE(r.decision_deadline,
                                 now() + interval '48 hours')              AS bid_window_closes_at,
                        r.created_at,
                        LEFT(r.client_id::TEXT, 8)                        AS consumer_ref,
                        s.monthly_income                                   AS client_monthly_income,
                        s.net_worth                                        AS client_net_worth,
                        s.debt_to_income_ratio                             AS client_debt_to_income_ratio
                    FROM  public.requests               r
                    LEFT JOIN public.client_financial_snapshot s ON s.client_id = r.client_id
                    WHERE r.status = 'open'
                    ORDER BY r.created_at DESC
                """
                result = app_conn.execute(text(_LEGACY_SQL))
                return _rows(result)
        except AppDatabaseUnavailable:
            return []

    return [_row(r) for r in rows]


# ── GET /marketplace/my-bids ──────────────────────────────────────────────────

@router.get("/my-bids")
async def list_my_bids(
    status: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """
    This institution's bids via marketplace.my_bids view (security_invoker).
    Includes request + product context — no separate call needed.
    """
    sql = "SELECT * FROM marketplace.my_bids"
    params: dict = {}
    if status:
        sql += " WHERE status = :st"
        params["st"] = status
    sql += " ORDER BY submitted_at DESC"

    rows = conn.execute(text(sql), params).fetchall()

    # Pre-migration fallback
    if not rows:
        fb_sql = "SELECT * FROM institution.institution_bids"
        fb_params: dict = {}
        if status:
            fb_sql += " WHERE status = :st"
            fb_params["st"] = status
        fb_sql += " ORDER BY submitted_at DESC"
        rows = conn.execute(text(fb_sql), fb_params).fetchall()

    return [_row(r) for r in rows]


# ── POST /marketplace/bids ────────────────────────────────────────────────────

@router.post("/bids")
async def submit_bid(
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Submit a bid to marketplace.bid.
    Requires: request_id, rate, rate_type, amount_offered, term_months.
    Optional: conditions, fee_structure, idempotency_key, rate_valid_days.
    """
    required = {"request_id", "rate", "rate_type", "amount_offered", "term_months"}
    missing = required - set(body.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    try:
        result = conn.execute(
            text("""
                INSERT INTO marketplace.bid
                    (request_id, institution_id, rate, rate_type,
                     rate_valid_days, amount_offered, term_months,
                     conditions, fee_structure, submitted_via, idempotency_key)
                VALUES
                    (:request_id, :institution_id, :rate, :rate_type,
                     :rate_valid_days, :amount_offered, :term_months,
                     CAST(:conditions AS jsonb), CAST(:fee_structure AS jsonb),
                     'portal', :idempotency_key)
                ON CONFLICT (institution_id, request_id, idempotency_key)
                DO NOTHING
                RETURNING id, status, submitted_at
            """),
            {
                "request_id":      body["request_id"],
                "institution_id":  inst_id,
                "rate":            body["rate"],
                "rate_type":       body.get("rate_type", "fixed"),
                "rate_valid_days": body.get("rate_valid_days"),
                "amount_offered":  body["amount_offered"],
                "term_months":     body["term_months"],
                "conditions":      json.dumps(body.get("conditions", {})),
                "fee_structure":   json.dumps(body.get("fee_structure", {})),
                "idempotency_key": body.get("idempotency_key"),
            },
        ).fetchone()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if result is None:
        # Duplicate — idempotency key already used
        return {"status": "duplicate", "message": "Bid already submitted for this request."}

    return {
        "id":           str(result.id),
        "status":       result.status,
        "submitted_at": result.submitted_at.isoformat(),
    }


# ── GET /marketplace/bids/{bid_id} ───────────────────────────────────────────

@router.get("/bids/{bid_id}")
async def get_bid(
    bid_id: str,
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Single bid detail including request and product context."""
    row = conn.execute(
        text("SELECT * FROM marketplace.my_bids WHERE id = :id"),
        {"id": bid_id},
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Bid not found.")
    return _row(row)
