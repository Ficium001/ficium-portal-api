# =============================================================================
# ficium-portal-api — Public router (server-to-server, no JWT)
# GET /public/requests/{request_id}/bids
#
# Called exclusively from the ficium Vercel backend (api/request-bids.ts).
# Authenticated by X-Service-Secret header — NOT a user JWT.
# Uses service_session() to bypass RLS so we can read cross-tenant bids
# for a specific request.
#
# Security layers:
#   1. X-Service-Secret constant-time check (shared secret, set via env var)
#   2. IDOR guard: consumer_id must match public.requests.client_id
# =============================================================================

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy import text

from ..core.config import settings
from ..core.db import service_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])


def _verify_secret(received: str) -> None:
    expected = settings.app_service_secret
    if not expected:
        raise HTTPException(status_code=503, detail="Service-to-service auth not configured.")
    if not hmac.compare_digest(received.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid service secret.")


@router.get("/requests/{request_id}/bids")
async def get_bids_for_request(
    request_id: str,
    consumer_id: str = Query(..., description="Client user ID — must own the request"),
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> list[dict]:
    """
    Return all submitted bids for a request.
    Secured by X-Service-Secret; no user JWT involved.
    Only called server-to-server from the ficium Vercel backend.
    """
    _verify_secret(x_service_secret)

    with service_session() as conn:
        # IDOR guard — confirm the consumer owns this request
        owner = conn.execute(
            text("SELECT client_id FROM public.requests WHERE id = :rid"),
            {"rid": request_id},
        ).fetchone()

        if owner is None:
            raise HTTPException(status_code=404, detail="Request not found.")

        if str(owner.client_id) != consumer_id:
            raise HTTPException(status_code=403, detail="Not the request owner.")

        # Fetch submitted bids, cheapest rate first
        rows = conn.execute(
            text("""
                SELECT
                    b.id,
                    b.request_id,
                    b.institution_id,
                    i.name          AS institution_name,
                    b.rate,
                    b.rate_type,
                    b.amount_offered,
                    b.term_months,
                    b.conditions,
                    b.submitted_at,
                    b.status
                FROM  institution.institution_bids b
                JOIN  institution.institutions     i ON i.id = b.institution_id
                WHERE b.request_id = :rid
                  AND b.status     = 'submitted'
                ORDER BY b.rate ASC
            """),
            {"rid": request_id},
        ).fetchall()

    return [dict(r._mapping) for r in rows]
