"""POST /integration/v1/events — the institution side's only inbound channel
from the borrower app. Contract v1 (ficium-integration).

Order of checks: integration configured -> signature (B2I keys) -> envelope
valid -> sent by the borrower side -> we have a handler for this type ->
inbox dedupe/ordering and handler run in ONE transaction.

A type with no handler yet returns 501 WITHOUT being recorded, so the sender
keeps retrying and nothing is silently dropped while the migration is under way.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import ficium_contract as fc
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.db import service_session

log = structlog.get_logger()
router = APIRouter(prefix="/integration/v1", tags=["integration"])

Handler = Callable[[Session, dict[str, Any]], bool | None]  # True: a new request (notify institutions)


def _handle_ping(session: Session, envelope: dict[str, Any]) -> None:
    log.info("integration_ping_received", event_id=envelope["id"], sequence=envelope["sequence"])


def _live(session: Session, envelope: dict[str, Any]) -> bool:
    """Apply the shadow to the live marketplace (db/034). True when the request is new."""
    return bool(session.execute(
        text("SELECT integration.apply_request_live(CAST(:r AS uuid))"),
        {"r": envelope["aggregate_id"]},
    ).scalar_one())


def _handle_request_published(session: Session, envelope: dict[str, Any]) -> bool:
    """A borrower request, new or changed: shadow first, then the live marketplace."""
    session.execute(
        text("SELECT integration.apply_request_published(CAST(:e AS jsonb))"),
        {"e": json.dumps(envelope)},
    )
    return _live(session, envelope)


def _handle_request_status_changed(session: Session, envelope: dict[str, Any]) -> bool:
    session.execute(
        text("SELECT integration.apply_request_status_changed(CAST(:e AS jsonb))"),
        {"e": json.dumps(envelope)},
    )
    _live(session, envelope)
    return False


HANDLERS: dict[str, Handler] = {
    "ping": _handle_ping,
    "request.published": _handle_request_published,
    "request.status_changed": _handle_request_status_changed,
}


def _record_and_apply(envelope: dict[str, Any]) -> tuple[str, bool]:
    """(inbox outcome, request is new). Commits when the session closes."""
    is_new = False
    with service_session() as s:
        outcome = str(
            s.execute(
                text("SELECT integration.record_inbox(:i, :t, :s, :a, :q)"),
                {
                    "i": envelope["id"],
                    "t": envelope["type"],
                    "s": envelope["source"],
                    "a": envelope["aggregate_id"],
                    "q": envelope["sequence"],
                },
            ).scalar_one()
        )
        if outcome == "apply":
            is_new = bool(HANDLERS[envelope["type"]](s, envelope))
    return outcome, is_new


def _notify_new_request(request_id: str) -> None:
    """request.new webhook to every institution with the product enabled.

    The sync this replaces filtered product_config on is_active / product_type, columns
    that don't exist, so this webhook was never sent."""
    from ..core.webhooks import dispatch_event

    with service_session() as s:
        req = s.execute(text("""
            SELECT r.id, r.product_id, r.amount, r.term_months, r.params ->> 'app_product_type' AS product_type
              FROM marketplace.request r WHERE r.id = CAST(:r AS uuid)
        """), {"r": request_id}).fetchone()
        if req is None:
            return
        institutions = [row.institution_id for row in s.execute(text("""
            SELECT DISTINCT institution_id FROM institution.product_config
             WHERE enabled AND product_id = :p
        """), {"p": req.product_id})]
    payload = {"request_id": str(req.id), "product_type": req.product_type,
               "amount": float(req.amount or 0), "term_months": req.term_months, "source": "integration"}
    for iid in institutions:
        try:
            asyncio.run(dispatch_event(str(iid), "request.new", payload))
        except Exception as exc:  # noqa: BLE001 - one institution's webhook must not stop the others
            log.warning("request_new_webhook_failed", institution_id=str(iid), error=str(exc))


def _err(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


@router.post("/events", response_model=None)
async def receive_event(request: Request) -> JSONResponse:
    keys = settings.integration_inbound_keys
    if not keys:
        return _err(503, "integration_disabled")
    raw = await request.body()
    try:
        fc.verify(request.headers.get(fc.SIGNATURE_HEADER, ""), raw, keys)
    except fc.ContractError as e:
        log.warning("integration_bad_signature", reason=str(e))
        return _err(401, "bad_signature")
    try:
        envelope = json.loads(raw)
        fc.validate_event(envelope)
    except (ValueError, fc.ContractError) as e:
        return _err(422, "contract_violation", str(e)[:500])
    if envelope["source"] != "borrower":
        return _err(403, "wrong_source")
    if envelope["type"] not in HANDLERS:
        return _err(501, "not_handled_yet", envelope["type"])
    outcome, is_new = await asyncio.to_thread(_record_and_apply, envelope)
    if is_new:
        # After commit: tell institutions licensed for the product.
        await asyncio.to_thread(_notify_new_request, envelope["aggregate_id"])
    return JSONResponse(status_code=200, content={"status": outcome, "id": envelope["id"]})
