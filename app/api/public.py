# =============================================================================
# ficium-portal-api — Public router (server-to-server, no JWT) v2 schema
# =============================================================================

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from ..core.config import settings
from ..core.db import AppDatabaseUnavailable, app_service_session, service_session

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
    consumer_id: str = Query(..., description="Client user ID - must own the request"),
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> list[dict]:
    """
    Return all submitted bids for a single request (server-to-server only).
    v2 path: marketplace.request + marketplace.bid (portal DB).
    Fallback: public.requests + institution_bids (app DB).
    """
    _verify_secret(x_service_secret)

    # ── v2 path: marketplace.request in portal DB ─────────────────────────────
    with service_session() as conn:
        owner = conn.execute(
            text("SELECT consumer_id FROM marketplace.request WHERE id = :rid"),
            {"rid": request_id},
        ).fetchone()

        if owner is not None:
            if str(owner.consumer_id) != consumer_id:
                raise HTTPException(status_code=403, detail="Not the request owner.")

            rows = conn.execute(
                text("""
                    SELECT
                        b.id, b.request_id, b.institution_id,
                        i.name          AS institution_name,
                        i.logo_url      AS institution_logo,
                        b.rate, b.rate_type, b.rate_valid_days,
                        b.amount_offered, b.term_months,
                        b.conditions, b.fee_structure,
                        b.status, b.submitted_at, b.expires_at
                    FROM  marketplace.bid         b
                    JOIN  institution.institution i ON i.id = b.institution_id
                    WHERE b.request_id = :rid
                      AND b.status IN ('submitted', 'under_review')
                    ORDER BY b.rate ASC
                """),
                {"rid": request_id},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    # ── Fallback: public.requests in app DB ───────────────────────────────────
    try:
        with app_service_session() as app_conn:
            owner_legacy = app_conn.execute(
                text("SELECT client_id FROM public.requests WHERE id = :rid"),
                {"rid": request_id},
            ).fetchone()

            if owner_legacy is None:
                raise HTTPException(status_code=404, detail="Request not found.")
            if str(owner_legacy.client_id) != consumer_id:
                raise HTTPException(status_code=403, detail="Not the request owner.")

            rows = app_conn.execute(
                text("""
                    SELECT
                        b.id, b.request_id, b.institution_id,
                        i.name      AS institution_name,
                        b.rate, b.rate_type, b.amount_offered,
                        b.term_months, b.conditions,
                        b.submitted_at, b.status
                    FROM  institution.institution_bids b
                    JOIN  institution.institutions     i ON i.id = b.institution_id
                    WHERE b.request_id = :rid
                      AND b.status     = 'submitted'
                    ORDER BY b.rate ASC
                """),
                {"rid": request_id},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class BulkBidsRequest(BaseModel):
    request_ids: list[str]
    consumer_id: str


@router.post("/requests/bids/bulk")
async def get_bids_bulk(
    body: Annotated[BulkBidsRequest, Body()],
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> dict[str, list[dict]]:
    """
    Bulk bid fetch — returns bids for multiple request IDs in ONE query.
    Replaces N parallel calls to /requests/{id}/bids from the consumer app.
    Ownership is enforced: only request IDs owned by consumer_id are returned.
    """
    _verify_secret(x_service_secret)

    if not body.request_ids:
        return {}

    result: dict[str, list[dict]] = {rid: [] for rid in body.request_ids}

    # ── v2 path: portal DB ────────────────────────────────────────────────────
    with service_session() as conn:
        owned = conn.execute(
            text("""
                SELECT id FROM marketplace.request
                WHERE id = ANY(:ids::uuid[]) AND consumer_id = :cid::uuid
            """),
            {"ids": body.request_ids, "cid": body.consumer_id},
        ).fetchall()
        owned_ids = [str(r.id) for r in owned]

        if owned_ids:
            rows = conn.execute(
                text("""
                    SELECT
                        b.id, b.request_id, b.institution_id,
                        i.name          AS institution_name,
                        i.logo_url      AS institution_logo,
                        b.rate, b.rate_type, b.rate_valid_days,
                        b.amount_offered, b.term_months,
                        b.conditions, b.fee_structure,
                        b.status, b.submitted_at, b.expires_at
                    FROM  marketplace.bid         b
                    JOIN  institution.institution i ON i.id = b.institution_id
                    WHERE b.request_id = ANY(:ids::uuid[])
                      AND b.status IN ('submitted', 'under_review')
                    ORDER BY b.rate ASC
                """),
                {"ids": owned_ids},
            ).fetchall()
            for r in rows:
                rid = str(r._mapping["request_id"])
                if rid in result:
                    result[rid].append(dict(r._mapping))

        # IDs not found in portal DB — check app DB fallback
        remaining = [rid for rid in body.request_ids if rid not in owned_ids]
        if not remaining:
            return result

    # ── Fallback: app DB ──────────────────────────────────────────────────────
    try:
        with app_service_session() as app_conn:
            owned_legacy = app_conn.execute(
                text("""
                    SELECT id FROM public.requests
                    WHERE id = ANY(:ids::uuid[]) AND client_id = :cid::uuid
                """),
                {"ids": remaining, "cid": body.consumer_id},
            ).fetchall()
            owned_legacy_ids = [str(r.id) for r in owned_legacy]

            if owned_legacy_ids:
                rows = app_conn.execute(
                    text("""
                        SELECT
                            b.id, b.request_id, b.institution_id,
                            i.name      AS institution_name,
                            b.rate, b.rate_type, b.amount_offered,
                            b.term_months, b.conditions,
                            b.submitted_at, b.status
                        FROM  institution.institution_bids b
                        JOIN  institution.institutions     i ON i.id = b.institution_id
                        WHERE b.request_id = ANY(:ids::uuid[])
                          AND b.status     = 'submitted'
                        ORDER BY b.rate ASC
                    """),
                    {"ids": owned_legacy_ids},
                ).fetchall()
                for r in rows:
                    rid = str(r._mapping["request_id"])
                    if rid in result:
                        result[rid].append(dict(r._mapping))

    except AppDatabaseUnavailable:
        pass  # fallback unavailable — return what we have from portal DB

    return result
