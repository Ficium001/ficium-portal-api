from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ..core.bidding import BidInput, BidPlacementError, place_bid
from ..core.db import service_session
from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..core.webhooks import dispatch_event
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

def _row(r) -> dict: return dict(r._mapping)

# ── GET /marketplace/requests ─────────────────────────────────────────────────
@router.get("/requests")
async def list_requests(
    product_type: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    sql = """
        SELECT r.id, r.consumer_ref, r.product_id,
               p.code    AS product_code,
               p.label   AS product_label,
               pf.label  AS product_family_label,
               r.country, r.currency, r.amount, r.term_months,
               r.params, r.metadata, r.status, r.allocation_mode,
               r.bid_window_opens_at, r.bid_window_closes_at,
               r.source, r.created_at,
               r.ficium_risk_tier, r.ficium_score,
               (SELECT COUNT(*) FROM marketplace.bid b
                WHERE b.request_id = r.id
                  AND b.status NOT IN ('withdrawn','rejected')) AS bid_count,
               (
                 SELECT json_agg(json_build_object(
                   'product_type',   cp.code,
                   'product_label',  cp.label,
                   'amount',         ra.amount
                 ) ORDER BY ra.sort_order)
                 FROM marketplace.request_allocation ra
                 JOIN catalog.product cp ON cp.id = ra.product_id
                 WHERE ra.request_id = r.id
               ) AS allocations
        FROM marketplace.request r
        JOIN catalog.product        p  ON p.id  = r.product_id
        JOIN catalog.product_family pf ON pf.id = p.family_id
        WHERE r.status IN ('open', 'bidding')
          AND r.bid_window_closes_at > now()
          AND r.bid_window_opens_at  <= now()
          -- A request this institution declined leaves its queue only (db/035).
          AND NOT EXISTS (SELECT 1 FROM marketplace.request_decline d
                           WHERE d.request_id = r.id
                             AND d.institution_id = (SELECT institution_id FROM institution.current_member_ctx_v2()))
    """
    params: dict = {}
    if product_type:
        sql += " AND p.code = :pt"
        params["pt"] = product_type
    sql += " ORDER BY r.bid_window_closes_at ASC"
    rows = conn.execute(text(sql), params).fetchall()
    # No fallback to the App DB here: marketplace.request (Portal DB) is the
    # single source of truth post Phase-1-sync, and is RLS-scoped by
    # institution + product_scope. A raw App DB fallback previously bypassed
    # both — every open request, every institution, every product, with no
    # filtering at all — and triggered any time a member's scoped query
    # legitimately returned zero rows (e.g. a product-restricted group with
    # no current matches). Removed rather than "fixed to be scoped" so there
    # is exactly one authorization path to maintain, not two that can drift.
    return [_row(r) for r in rows]

# ── GET /marketplace/my-bids ──────────────────────────────────────────────────
@router.get("/my-bids")
async def list_my_bids(
    status: str | None = Query(default=None),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    if claims.get("user_role") in INSTITUTION_ADMIN_ROLES:
        return []
    sql = "SELECT * FROM marketplace.my_bids"
    params: dict = {}
    if status:
        sql += " WHERE status = :st"
        params["st"] = status
    sql += " ORDER BY submitted_at DESC"
    return [_row(r) for r in conn.execute(text(sql), params).fetchall()]

# ── POST /marketplace/bids ────────────────────────────────────────────────────
@router.post("/bids")
async def submit_bid(
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    required = {"request_id", "rate", "rate_type", "amount_offered", "term_months"}
    missing = required - set(body.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    bid = BidInput(
        request_id=body["request_id"],
        institution_id=str(inst_id),
        rate=body["rate"],
        rate_type=body.get("rate_type", "fixed"),
        amount_offered=body["amount_offered"],
        term_months=body["term_months"],
        rate_valid_days=body.get("rate_valid_days"),
        conditions=body.get("conditions", {}),
        fee_structure=body.get("fee_structure", {}),
        idempotency_key=body.get("idempotency_key"),
        submitted_via="portal",
    )
    try:
        result = place_bid(conn, bid)
    except BidPlacementError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    conn.commit()

    if result.duplicate:
        return {"status": "duplicate", "message": "Bid already submitted."}
    return {
        "id": result.id,
        "status": result.status,
        "submitted_at": result.submitted_at.isoformat() if result.submitted_at else None,
    }

# ── GET /marketplace/bids/{bid_id} ───────────────────────────────────────────
@router.get("/bids/{bid_id}")
async def get_bid(bid_id: str, conn: Session = Depends(tenant_conn)) -> dict:
    row = conn.execute(text("SELECT * FROM marketplace.my_bids WHERE id = :id"), {"id": bid_id}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Bid not found.")
    return _row(row)

@router.get("/bids/{bid_id}/reveal")
async def get_bid_reveal(
    bid_id: str,
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Phase 2 borrower identity for an accepted bid.

    This is the endpoint InstitutionBids.tsx's BorrowerRevealPanel has always
    called — it never existed, so the panel's query 404'd and, since the
    component treats "no data" as "nothing to show" (`if (!reveal) return
    null`), every accepted bid silently rendered without its reveal section.
    No error was visible anywhere; this is the fix.

    marketplace.bid_acceptance is written once, at acceptance time, by
    accept_bid() (see db/004_accept_bid_reveal.sql) — the same row
    pipeline.py already reads for a pipeline's Phase 2 reveal. This is the
    per-bid equivalent for a caller who only has a bid id, not a pipeline id.
    """
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    row = conn.execute(
        text("""
            SELECT full_name, email, phone, address, date_of_birth,
                   document_number, revealed_at
            FROM marketplace.bid_acceptance
            WHERE bid_id = :bid_id AND institution_id = :iid
        """),
        {"bid_id": bid_id, "iid": institution_id},
    ).fetchone()

    if row is None:
        # Distinguish "not accepted yet" from "not your bid" no further than the
        # existing get_bid() above does — same 404 shape either way.
        raise HTTPException(status_code=404, detail="No reveal available for this bid.")

    return _row(row)


# ── POST /marketplace/requests/{request_id}/reject ──────────────────────────
@router.post("/requests/{request_id}/reject")
async def reject_request(
    request_id: str,
    body: dict = Body(default={}),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    This institution declines a request (distinct from simply not bidding).

    Per institution (db/035): the request leaves this institution's queue only and stays open
    to every other one; an institution with a live bid must withdraw it instead. The borrower
    gets an anonymous request.declined event (contract 1.8.0). This used to write straight into
    the borrower database (public.reject_request), which closed the borrower's request and
    cancelled it for every institution on the first decline.
    """
    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")
    reason = (body.get("reason") or "").strip() or None
    if reason and len(reason) > 500:
        raise HTTPException(status_code=422, detail="Reason too long (max 500 chars).")

    # RLS-scoped visibility: only a request this institution can see can be declined.
    if conn.execute(text("SELECT 1 FROM marketplace.request WHERE id = CAST(:rid AS uuid)"),
                    {"rid": request_id}).fetchone() is None:
        raise HTTPException(status_code=404, detail="Request not found.")

    errors = {
        "DECLINE_NOT_FOUND": (404, "Request not found."),
        "DECLINE_NOT_OPEN": (409, "This request is no longer open."),
        "DECLINE_HAS_BID": (409, "You have an offer on this request. Withdraw it instead."),
        "DECLINE_ALREADY": (409, "You already declined this request."),
        "DECLINE_REASON_TOO_LONG": (422, "Reason too long (max 500 chars)."),
    }
    try:
        with service_session() as svc:
            svc.execute(
                text("SELECT marketplace.decline_request(CAST(:rid AS uuid), CAST(:iid AS uuid), "
                     "CAST(:actor AS uuid), :reason)"),
                {"rid": request_id, "iid": inst_id, "actor": claims.get("sub"), "reason": reason},
            )
            svc.execute(
                text("""
                    INSERT INTO audit.event
                        (id, occurred_at, actor_id, actor_type, actor_email, actor_role,
                         institution_id, action, resource_type, resource_id, outcome, metadata)
                    VALUES
                        (gen_random_uuid(), now(), CAST(:actor_id AS uuid), 'member', :actor_email, :actor_role,
                         CAST(:inst_id AS uuid), 'request.decline', 'marketplace_request', CAST(:rid AS uuid),
                         'success', jsonb_build_object('reason', CAST(:reason AS text)))
                """),
                {"actor_id": claims.get("sub"), "actor_email": claims.get("email"),
                 "actor_role": claims.get("user_role"), "inst_id": inst_id, "rid": request_id,
                 "reason": reason},
            )
    except DBAPIError as exc:
        msg = str(getattr(exc, "orig", exc))
        hit = next((v for k, v in errors.items() if k in msg), None)
        if hit:
            raise HTTPException(status_code=hit[0], detail=hit[1]) from exc
        raise

    asyncio.create_task(dispatch_event(str(inst_id), "request.rejected", {
        "request_id": request_id,
        "reason": reason,
    }))
    return {"ok": True, "request_id": request_id, "status": "declined"}

# ── Closing expired bid windows (maintenance loop only) ──────────────────────
# Stable id for the transaction-scoped advisory lock: only one caller (any replica, the scheduler
# or this endpoint) closes windows at a time.
_CLOSE_EXPIRED_LOCK_KEY = 7240011


def close_expired_once() -> dict[str, int]:
    """Close every bid window that has passed. Blocking; call via asyncio.to_thread.

    Semantics are those of marketplace.close_expired_windows() (a set-returning function):
      * with active bids  -> request becomes 'closed'  (bids stay alive for the borrower to choose)
      * no active bids    -> request becomes 'expired'
    Nothing is rejected here, so no bid.rejected webhook is sent. (The previous version pre-selected
    bids on requests with status 'open' - a status the portal never uses - so its bid.rejected loop never
    fired; "fixing" that filter to 'bidding' would have sent FALSE rejections for bids that stay alive.)
    """
    with service_session() as conn:
        got = conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _CLOSE_EXPIRED_LOCK_KEY}
        ).scalar()
        if not got:
            return {"closed": 0, "expired": 0, "skipped": 1}
        rows = conn.execute(
            text("SELECT request_id, new_status FROM marketplace.close_expired_windows()")
        ).fetchall()
    return {
        "closed": sum(1 for r in rows if r.new_status == "closed"),
        "expired": sum(1 for r in rows if r.new_status == "expired"),
        "skipped": 0,
    }


# (removed) POST /marketplace/close-expired: a manual trigger guarded by the secret shared with
# the borrower app. The maintenance loop (app/maintenance.py) closes windows on its own.


# ── POST /marketplace/sync-requests ──────────────────────────────────────────
# Phase 1 helpers ─────────────────────────────────────────────────────────────


# (removed) POST /marketplace/sync-requests and GET /marketplace/sync-health, with the phase1
# builders they used. The portal read borrower requests straight out of the App DB. Requests now
# arrive only as contract events (request.published / request.status_changed), applied live by
# integration.apply_request_live (db/034). Phase 1 is built on the borrower side; the contract's
# phase1 schema has no employer field, so that rule is enforced at the boundary.
