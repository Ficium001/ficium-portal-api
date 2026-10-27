# =============================================================================
# ficium-portal-api — Nav badge counts in one request
#
#   GET /portal/badges
#
# The portal shell shows a count beside each nav item. It used to fetch nine
# full lists (marketplace, bids, active and completed deals, envelopes, two
# pending-action queues, the approval inbox) on every page, several re-polled
# every 30 s, and browsers allow 6 requests per host at a time — so the page
# the user opened waited behind them.
#
# This calls the SAME list functions in-process, on one tenant connection, and
# returns only their counts. The filters, RLS scoping and permission checks are
# therefore identical to the list screens by construction; nothing is
# re-implemented that could drift. A count the caller may not see is null.
# =============================================================================
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..deps import _institution_modules, current_claims, tenant_conn
from . import approval_engine, approvals, esign, marketplace, pipeline

log = structlog.get_logger()

router = APIRouter(prefix="/portal", tags=["portal"])


async def _count(
    conn: Session, name: str,
    fn: Callable[[], Awaitable[list[dict]]], keep: Callable[[dict], bool],
) -> int | None:
    # Savepoint per count: a DB error in one queue would otherwise abort the
    # shared transaction and take every later count down with it.
    sp = conn.begin_nested()
    try:
        rows = await fn()
        sp.commit()
    except HTTPException:
        sp.rollback()
        return None  # not licensed / not permitted: same answer the list would give
    except Exception:  # one broken queue must not blank every badge
        sp.rollback()
        log.exception("badge_count_failed", badge=name)
        return None
    return sum(1 for r in rows if keep(r))


@router.get("/badges")
async def badges(
    claims: dict[str, Any] = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict[str, int | None]:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    has_pipeline = "pipeline" in _institution_modules(str(iid))

    async def deals(status: str) -> list[dict]:
        if not has_pipeline:
            raise HTTPException(status_code=403)
        return await pipeline.list_pipelines(status=status, claims=claims, conn=conn)

    def submitted(r: dict) -> bool:
        return r.get("status") == "submitted"

    def pending(r: dict) -> bool:
        # Only what THIS user may decide: four-eyes bars approving your own, so
        # your own submissions are waiting on someone else, not on you.
        return r.get("action_status") == "pending" and not r.get("is_mine")

    return {
        "open_requests": await _count(
            conn, "open_requests",
            lambda: marketplace.list_requests(product_type=None, conn=conn),
            lambda r: True),  # the list already keeps open, in-window requests
        "live_bids": await _count(
            conn, "live_bids",
            lambda: marketplace.list_my_bids(status=None, claims=claims, conn=conn),
            submitted),
        "active_deals": await _count(conn, "active_deals", lambda: deals("active"), lambda r: True),
        "awaiting_booking": await _count(
            conn, "awaiting_booking", lambda: deals("completed"), lambda r: not r.get("booked")),
        "awaiting_signature": await _count(
            conn, "awaiting_signature",
            lambda: esign.list_envelopes(conn=conn, claims=claims),
            lambda r: r.get("status") in ("sent", "partially_signed")),
        "pending_bid_actions": await _count(
            conn, "pending_bid_actions",
            lambda: approvals.list_pending_actions(scope="bids", conn=conn, claims=claims),
            pending),
        "pending_internal": await _count(
            conn, "pending_internal",
            lambda: approvals.list_pending_actions(scope="internal", conn=conn, claims=claims),
            pending),
        "awaiting_my_vote": await _count(
            conn, "awaiting_my_vote",
            lambda: approval_engine.my_inbox(conn=conn, claims=claims),
            lambda r: not r.get("my_vote")),
    }
