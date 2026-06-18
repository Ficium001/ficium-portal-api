# =============================================================================
# ficium-portal-api — Public router (server-to-server, no JWT) v2 schema
#
# GET /public/requests/{request_id}/bids
#   Reads marketplace.bid + institution.institution (v2 names).
#   Falls back to institution.institution_bids + institution.institutions
#   if marketplace.bid is empty (pre-migration state).
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
    Return all submitted bids for a request (server-to-server only).
    Secured by X-Service-Secret. IDOR guard: consumer_id must own the request.

    v2: reads marketplace.request + marketplace.bid + institution.institution.
    Pre-migration fallback: reads public.requests + institution.institution_bids.
    """
    _verify_secret(x_service_secret)

    with service_session() as conn:
        # ── v2 path: marketplace.request ─────────────────────────────────────
        owner = conn.execute(
            text("SELECT consumer_id FROM marketplace.request WHERE id = :rid"),
            {"rid": request_id},
        ).fetchone()

        if owner is not None:
            # v2 — verify ownership
            if str(owner.consumer_id) != consumer_id:
                raise HTTPException(status_code=403, detail="Not the request owner.")

            rows = conn.execute(
                text("""
                    SELECT
                        b.id,
                        b.request_id,
                        b.institution_id,
                        i.name          AS institution_name,
                        i.logo_url      AS institution_logo,
                        b.rate,
                        b.rate_type,
                        b.rate_valid_days,
                        b.amount_offered,
                        b.term_months,
                        b.conditions,
                        b.fee_structure,
                        b.status,
                        b.submitted_at,
                        b.expires_at
                    FROM  marketplace.bid          b
                    JOIN  institution.institution  i ON i.id = b.institution_id
                    WHERE b.request_id = :rid
                      AND b.status IN ('submitted', 'under_review')
                    ORDER BY b.rate ASC
                """),
                {"rid": request_id},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

        # ── Pre-migration fallback: public.requests (app DB reads via service conn)
        owner_legacy = conn.execute(
            text("SELECT client_id FROM public.requests WHERE id = :rid"),
            {"rid": request_id},
        ).fetchone()

        if owner_legacy is None:
            raise HTTPException(status_code=404, detail="Request not found.")
        if str(owner_legacy.client_id) != consumer_id:
            raise HTTPException(status_code=403, detail="Not the request owner.")

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
