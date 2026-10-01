"""POST /integration/v1/events — the institution side's only inbound channel
from the borrower app. Contract v1 (ficium-contract).

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

Handler = Callable[[Session, dict[str, Any]], None]


def _handle_ping(session: Session, envelope: dict[str, Any]) -> None:
    log.info("integration_ping_received", event_id=envelope["id"], sequence=envelope["sequence"])


HANDLERS: dict[str, Handler] = {
    "ping": _handle_ping,
}


def _record_and_apply(envelope: dict[str, Any]) -> str:
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
            HANDLERS[envelope["type"]](s, envelope)
        return outcome


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
    outcome = await asyncio.to_thread(_record_and_apply, envelope)
    return JSONResponse(status_code=200, content={"status": outcome, "id": envelope["id"]})
