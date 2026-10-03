"""POST /integration/v1/acceptances - step 5 of the integration contract: the ONE synchronous call.

The borrower side sends only the identity fields the borrower agreed to release; this side never reads the
borrower database. OFF unless INTEGRATION_ACCEPTANCE_ENABLED=true, and signed with its own key
(INTEGRATION_ACCEPTANCE_VERIFY_KEYS), separate from the event keys.

Order of checks: enabled -> signature -> Idempotency-Key -> contract -> (one transaction, serialised per key)
replay? -> request exists -> caller owns it (secret-keyed anonymous id from the step 3 shadow) -> still open ->
marketplace.accept_bid() (the same atomic function the legacy endpoint uses) -> contract response -> log.
Side effects (bank notification + webhooks) run once, only on the first successful call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

import ficium_contract as fc
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ..core.config import settings
from ..core.db import service_session
from .public import after_acceptance

router = APIRouter(prefix="/integration/v1", tags=["integration"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _e(code: str, detail: str = "") -> dict[str, str]:
    return {"error": code, "detail": detail}


def _contract_response(body: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    email = res.get("contact_email")
    return {
        "bid_id": body["bid_id"],
        "request_id": body["request_id"],
        "pipeline_id": str(res["pipeline_id"]),
        "institution": {
            "institution_id": str(res["institution_id"]),
            "name": res.get("institution_name") or "",
            "contact_email": email if isinstance(email, str) and _EMAIL.match(email) else None,
            "contact_phone": res.get("contact_phone"),
            # beyond the contract minimum, so the borrower app can show exactly what it shows today
            "legal_name": res.get("legal_name"),
            "contact_person": res.get("contact_person"),
            "logo_url": res.get("logo_url"),
        },
        "deal": {
            "amount": res["amount_offered"],
            "rate": res["rate"],
            "term_months": res["term_months"],
            "rate_type": res.get("rate_type"),
        },
    }


def _decide(conn: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    row = conn.execute(
        text(
            "SELECT r.consumer_id, r.status, s.anon_borrower_id FROM marketplace.request r "
            "LEFT JOIN integration.request_shadow s ON s.request_id = r.id WHERE r.id = :rid"
        ),
        {"rid": body["request_id"]},
    ).fetchone()
    if row is None:
        return 404, _e("request_not_found"), None
    if row.anon_borrower_id is None or str(row.anon_borrower_id) != body["anon_borrower_id"]:
        return 403, _e("not_the_request_owner"), None
    if row.status in ("accepted", "cancelled", "expired"):
        return 409, _e(f"request_{row.status}"), None

    ri = body["released_identity"]
    phase2 = {
        "full_name": ri["full_name"],
        "email": ri.get("email", ""),
        "phone": ri.get("phone"),
        "address": ri.get("address"),
        "date_of_birth": ri.get("date_of_birth"),
        "document_number": ri.get("document_number"),
    }
    try:
        with conn.begin_nested():
            res = conn.execute(
                text(
                    "SELECT marketplace.accept_bid(CAST(:rid AS uuid), CAST(:bid AS uuid), "
                    "CAST(:cons AS uuid), CAST(:p AS jsonb)) AS r"
                ),
                {
                    "rid": body["request_id"],
                    "bid": body["bid_id"],
                    "cons": str(row.consumer_id),
                    "p": json.dumps(phase2),
                },
            ).scalar_one()
    except DBAPIError as exc:
        return 409, _e("bid_not_acceptable", str(getattr(exc, "orig", exc))[:300]), None

    result = dict(res)
    response = _contract_response(body, result)
    # Never answer outside the contract. Raising here rolls back the whole transaction,
    # including the acceptance itself.
    fc.validate_acceptance_response(response)
    return 200, response, result


def _accept(
    idem: str, body_hash: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    with service_session() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "acceptance:" + idem}
        )
        prev = conn.execute(
            text(
                "SELECT body_sha256, status_code, response FROM integration.acceptance_log "
                "WHERE idempotency_key = :k"
            ),
            {"k": idem},
        ).fetchone()
        if prev is not None:
            if prev.body_sha256 != body_hash:
                return 409, _e("idempotency_key_reused", "Same key, different body."), None
            return int(prev.status_code), dict(prev.response), None
        status, payload, result = _decide(conn, body)
        conn.execute(
            text(
                "INSERT INTO integration.acceptance_log "
                "(idempotency_key, body_sha256, request_id, bid_id, status_code, response) "
                "VALUES (:k, :h, CAST(:rid AS uuid), CAST(:bid AS uuid), :s, CAST(:r AS jsonb))"
            ),
            {
                "k": idem,
                "h": body_hash,
                "rid": body["request_id"],
                "bid": body["bid_id"],
                "s": status,
                "r": json.dumps(payload, default=str),
            },
        )
        return status, payload, result


@router.post("/acceptances", response_model=None)
async def create_acceptance(request: Request) -> JSONResponse:
    keys = settings.integration_acceptance_keys
    if not settings.integration_acceptance_enabled or not keys:
        return JSONResponse(status_code=503, content=_e("acceptance_disabled"))
    raw = await request.body()
    try:
        fc.verify(request.headers.get(fc.SIGNATURE_HEADER, ""), raw, keys)
    except fc.ContractError:
        return JSONResponse(status_code=401, content=_e("bad_signature"))
    idem = request.headers.get("Idempotency-Key", "").strip()
    if not 8 <= len(idem) <= 200:
        return JSONResponse(status_code=400, content=_e("idempotency_key_required"))
    try:
        body = json.loads(raw)
        fc.validate_acceptance_request(body)
    except (ValueError, fc.ContractError) as e:
        return JSONResponse(status_code=422, content=_e("contract_violation", str(e)[:500]))

    status, payload, result = await asyncio.to_thread(
        _accept, idem, hashlib.sha256(raw).hexdigest(), body
    )
    if status == 200 and result is not None:
        await after_acceptance(result, body["request_id"], body["bid_id"])
    return JSONResponse(status_code=status, content=json.loads(json.dumps(payload, default=str)))
