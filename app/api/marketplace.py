from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.bidding import BidInput, BidPlacementError, place_bid
from ..core.config import settings
from ..core.db import AppDatabaseUnavailable, app_service_session, service_session
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
    Institution explicitly declines a marketplace request (distinct from
    simply not bidding on it). The App DB's reject_request() SECURITY
    DEFINER function is the single authority for the status transition,
    the borrower notification, and the audit trail (request_decisions) —
    this endpoint only validates institution visibility/eligibility first,
    then mirrors the decline onto the Portal DB copy so it drops out of
    every institution's open queue immediately.
    """
    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    reason = (body.get("reason") or "").strip() or None
    if reason and len(reason) > 500:
        raise HTTPException(status_code=422, detail="Reason too long (max 500 chars).")

    # RLS-scoped visibility/state pre-check before the App DB round-trip.
    req_row = conn.execute(
        text("SELECT id, status FROM marketplace.request WHERE id = :rid"),
        {"rid": request_id},
    ).fetchone()
    if req_row is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if req_row.status != "bidding":
        raise HTTPException(status_code=409, detail=f"Request is not open (status: {req_row.status}).")

    try:
        with app_service_session() as app_conn:
            result = app_conn.execute(
                text("SELECT public.reject_request(CAST(:rid AS uuid), CAST(:iid AS uuid), :reason) AS result"),
                {"rid": request_id, "iid": inst_id, "reason": reason},
            ).fetchone()
    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    payload = result.result if result else {"ok": False, "error": "no_response"}
    if not payload.get("ok"):
        err = payload.get("error", "reject_failed")
        status_code = 409 if err in ("request_not_open", "request_not_found") else 400
        raise HTTPException(status_code=status_code, detail=err)

    # Mirror onto the Portal DB copy — don't wait on the next pull-sync,
    # which only ever ingests App DB requests that are still 'open'.
    conn.execute(
        text("""
            UPDATE marketplace.request
            SET status = 'cancelled', cancelled_at = now(), cancellation_reason = :reason
            WHERE id = :rid
        """),
        {"rid": request_id, "reason": reason or "Declined by institution"},
    )

    asyncio.create_task(dispatch_event(str(inst_id), "request.rejected", {
        "request_id": request_id,
        "reason": reason,
    }))

    # Best-effort audit trail entry — never blocks or fails the reject itself.
    try:
        with service_session() as audit_conn:
            audit_conn.execute(
                text("""
                    INSERT INTO audit.event
                        (id, occurred_at, actor_id, actor_type, actor_email, actor_role,
                         institution_id, action, resource_type, resource_id, outcome, metadata)
                    VALUES
                        (gen_random_uuid(), now(), CAST(:actor_id AS uuid), 'member', :actor_email, :actor_role,
                         CAST(:inst_id AS uuid), 'request.reject', 'marketplace_request', CAST(:rid AS uuid),
                         'success', jsonb_build_object('reason', :reason))
                """),
                {
                    "actor_id": claims.get("sub"),
                    "actor_email": claims.get("email"),
                    "actor_role": claims.get("user_role"),
                    "inst_id": inst_id,
                    "rid": request_id,
                    "reason": reason,
                },
            )
    except Exception:
        pass  # audit is best-effort; core action already committed

    return {"ok": True, "request_id": request_id, "status": "rejected"}

# ── POST /marketplace/close-expired ──────────────────────────────────────────
def _verify_service_secret(received: str) -> None:
    expected = settings.app_service_secret
    if not expected:
        raise HTTPException(status_code=503, detail="Service secret not configured.")
    if not hmac.compare_digest(received.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid service secret.")

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


@router.post("/close-expired")
async def close_expired(
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> dict:
    _verify_service_secret(x_service_secret)
    counts = await asyncio.to_thread(close_expired_once)
    total = counts["closed"] + counts["expired"]
    return {
        "closed": total,  # total windows closed; kept as an int for existing callers
        "windows_closed_with_bids": counts["closed"],
        "windows_expired": counts["expired"],
        "bids_rejected": 0,
        "skipped_another_run_in_progress": bool(counts["skipped"]),
        "message": f"Closed {total} expired bid window(s).",
    }

# ── POST /marketplace/sync-requests ──────────────────────────────────────────
# Phase 1 helpers ─────────────────────────────────────────────────────────────


# (removed) POST /marketplace/sync-requests and GET /marketplace/sync-health, with the phase1
# builders they used. The portal read borrower requests straight out of the App DB. Requests now
# arrive only as contract events (request.published / request.status_changed), applied live by
# integration.apply_request_live (db/034). Phase 1 is built on the borrower side; the contract's
# phase1 schema has no employer field, so that rule is enforced at the boundary.
