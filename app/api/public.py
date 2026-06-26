# =============================================================================
# ficium-portal-api — Public router (server-to-server, no JWT) v2 schema
# =============================================================================

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid as uuid_mod
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


def _anon_uuid(real_id: str) -> str:
    """Derive the anonymised consumer UUID stored in Portal DB from the real Supabase user ID."""
    return str(uuid_mod.UUID(hashlib.md5((real_id + ":ficium-anon-v1:").encode()).hexdigest()))


@router.get("/requests/{request_id}/bids")
async def get_bids_for_request(
    request_id: str,
    consumer_id: str = Query(..., description="Client user ID - must own the request"),
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> list[dict]:
    _verify_secret(x_service_secret)

    anon_id = _anon_uuid(consumer_id)

    # ── v2 path: portal DB ────────────────────────────────────────────────────
    with service_session() as conn:
        owner = conn.execute(
            text("SELECT consumer_id FROM marketplace.request WHERE id = :rid"),
            {"rid": request_id},
        ).fetchone()

        if owner is not None:
            if str(owner.consumer_id) != anon_id:
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

    # ── Fallback: app DB ──────────────────────────────────────────────────────
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

    anon_cid = _anon_uuid(body.consumer_id)

    # ── v2 path: portal DB ────────────────────────────────────────────────────
    with service_session() as conn:
        owned = conn.execute(
            text("""
                SELECT id FROM marketplace.request
                WHERE id = ANY(CAST(:ids AS uuid[])) AND consumer_id = CAST(:cid AS uuid)
            """),
            {"ids": body.request_ids, "cid": anon_cid},
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
                    WHERE b.request_id = ANY(CAST(:ids AS uuid[]))
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
                    WHERE id = ANY(CAST(:ids AS uuid[])) AND client_id = CAST(:cid AS uuid)
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
                        WHERE b.request_id = ANY(CAST(:ids AS uuid[]))
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


class AcceptBidRequest(BaseModel):
    bid_id:      str
    consumer_id: str   # real Supabase user ID — we derive anon UUID here


@router.post("/requests/{request_id}/accept-bid")
async def accept_bid(
    request_id: str,
    body: Annotated[AcceptBidRequest, Body()],
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> dict:
    """
    Phase 2 reveal — server-to-server only.

    1. Verify ownership (anon UUID check against Portal DB).
    2. Fetch borrower PII from App DB.
    3. Call marketplace.accept_bid() atomically:
         - winning bid → accepted
         - all others  → rejected
         - request     → accepted + winning_bid_id
         - bid_acceptance row with Phase 2 PII written
    4. Return institution contact info to caller.
    """
    _verify_secret(x_service_secret)

    anon_id = _anon_uuid(body.consumer_id)

    # ── Ownership guard ───────────────────────────────────────────────────────
    with service_session() as conn:
        req_row = conn.execute(
            text("""
                SELECT r.consumer_id, r.status
                FROM marketplace.request r
                WHERE r.id = :rid
            """),
            {"rid": request_id},
        ).fetchone()

    if req_row is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if str(req_row.consumer_id) != anon_id:
        raise HTTPException(status_code=403, detail="Not the request owner.")
    if req_row.status in ("accepted", "cancelled", "expired"):
        raise HTTPException(status_code=409, detail=f"Request already {req_row.status}.")

    # ── Fetch Phase 2 PII from App DB ─────────────────────────────────────────
    try:
        with app_service_session() as app_conn:
            client = app_conn.execute(
                text("""
                    SELECT
                        c.full_name,
                        c.email,
                        c.phone,
                        c.date_of_birth,
                        CONCAT_WS(', ', c.address_line_1, c.city, c.postal_code) AS address,
                        k.document_number
                    FROM public.clients c
                    LEFT JOIN (
                        SELECT DISTINCT ON (user_id) user_id, document_number
                        FROM public.kyc_submissions
                        WHERE status = 'approved'
                        ORDER BY user_id, created_at DESC
                    ) k ON k.user_id = c.id
                    WHERE c.id = :uid
                """),
                {"uid": body.consumer_id},
            ).fetchone()
    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail="App DB unavailable.") from exc

    if client is None:
        raise HTTPException(status_code=404, detail="Client profile not found.")

    phase2 = {
        "full_name":       client.full_name or "",
        "email":           client.email or "",
        "phone":           client.phone,
        "address":         client.address,
        "date_of_birth":   client.date_of_birth.isoformat() if client.date_of_birth else None,
        "document_number": client.document_number,
    }

    # ── Atomic accept — writes reveal + transitions all statuses ──────────────
    import json as _json
    with service_session() as conn:
        try:
            result = conn.execute(
                text("""
                    SELECT marketplace.accept_bid(
                        :request_id,
                        :bid_id,
                        :consumer_id,
                        CAST(:phase2 AS jsonb)
                    ) AS result
                """),
                {
                    "request_id":  request_id,
                    "bid_id":      body.bid_id,
                    "consumer_id": anon_id,
                    "phase2":      _json.dumps(phase2),
                },
            ).fetchone()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return dict(result.result) if result else {}
