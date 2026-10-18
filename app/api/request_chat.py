# =============================================================================
# Request chat — institution side
#
# Per-lender threads through the contract only (1.9.1). This module used to read
# and write the borrower database (public.request_messages) directly, storing the
# sending staff member's id there. Now:
#
#   - Threads live in marketplace.request_message (db/036), this side's copy.
#     Borrower messages arrive as chat.message events (integration.py); ours
#     leave as chat.message events from marketplace.send_institution_message().
#   - Templates come from the contract (ficium_contract.chat_templates), and
#     bodies are rendered with its renderer, the same one the borrower side uses.
#   - The rules come from this side's own data (who won = an accepted bid):
#     a thread closes once another institution's offer is accepted; free text
#     only for the institution whose offer was accepted.
#
# Invariants kept:
#   1. Thread isolation: every query filters on the CALLER's institution_id
#      from the JWT, never a client-supplied value.
#   2. No person's identity crosses: chat.message carries no staff or borrower
#      id; member_id stays on this side.
# =============================================================================
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import ficium_contract as fc
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ..core.db import service_session
from ..deps import current_claims

router = APIRouter(prefix="/marketplace/requests", tags=["marketplace"])

MAX_BODY_LEN = 2000
_COLS = "id, request_id, sender_side, body, kind, template_code, params, created_at"
_ERRORS = {
    "CHAT_REQUEST_NOT_FOUND": (404, "Request not found."),
    "CHAT_CLOSED": (409, "This conversation is closed: another lender's offer was accepted."),
    "CHAT_FREE_TEXT_NOT_ALLOWED": (409, "Free text opens once the borrower accepts your offer."),
}


def _institution_id(claims: dict) -> str:
    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")
    return str(inst_id)


def _mask(m: Any) -> dict:
    """A message as this institution sees it. Never a person's id."""
    row = m._mapping if hasattr(m, "_mapping") else m
    side = row["sender_side"]
    return {
        "id": str(row["id"]),
        "request_id": str(row["request_id"]),
        "sender_type": "institution" if side == "institution" else "client",
        "sender_label": "You" if side == "institution" else "Borrower",
        "body": row["body"],
        "kind": row["kind"],
        "template_code": row["template_code"],
        "params": row["params"],
        "created_at": row["created_at"],
    }


def _state(conn: Any, request_id: str, institution_id: str) -> dict:
    row = conn.execute(
        text("SELECT is_open, is_winner FROM marketplace.chat_thread_state(CAST(:r AS uuid), CAST(:i AS uuid))"),
        {"r": request_id, "i": institution_id},
    ).one()
    return {"is_open": bool(row.is_open), "is_winner": bool(row.is_winner),
            "can_send_free_text": bool(row.is_open and row.is_winner)}


@router.get("/message-templates")
async def list_message_templates(claims: dict = Depends(current_claims)) -> list[dict]:
    """Structured messages this institution may send (from the contract)."""
    _institution_id(claims)
    return [{k: t[k] for k in ("code", "label", "body_template", "params_schema", "sort_order")}
            for t in fc.chat_templates("institution")]


@router.get("/{request_id}/messages")
async def list_messages(request_id: UUID, claims: dict = Depends(current_claims)) -> dict:
    """This institution's thread on one request (scoped to the caller's institution)."""
    inst_id = _institution_id(claims)
    with service_session() as conn:
        rows = conn.execute(
            text(f"""
                SELECT {_COLS} FROM marketplace.request_message
                 WHERE request_id = CAST(:rid AS uuid) AND institution_id = CAST(:iid AS uuid)
                 ORDER BY created_at
            """),
            {"rid": str(request_id), "iid": inst_id},
        ).fetchall()
        state = _state(conn, str(request_id), inst_id)
    return {"messages": [_mask(r) for r in rows], **state}


@router.post("/{request_id}/messages", status_code=201)
async def send_message(
    request_id: UUID,
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    """Post a message onto this institution's thread.

    institution_id and member id come from the JWT, never the body. Structured
    messages are rendered with the contract renderer (what the borrower side
    re-renders and checks); the rules are enforced in send_institution_message.
    """
    inst_id = _institution_id(claims)
    member_id = claims.get("sub") or claims.get("member_id")
    if not member_id:
        raise HTTPException(status_code=403, detail="No member context.")

    kind = (body.get("kind") or "structured").strip()
    if kind not in ("structured", "free"):
        raise HTTPException(status_code=422, detail="kind must be 'structured' or 'free'.")

    template_code: str | None = body.get("template_code")
    params: dict = body.get("params") or {}
    if kind == "structured":
        if not template_code:
            raise HTTPException(status_code=422, detail="template_code is required for a structured message.")
        try:
            text_body, params = fc.render_chat_message(template_code, params, "institution")
        except fc.ContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        text_body = (body.get("body") or "").strip()
        if not text_body:
            raise HTTPException(status_code=422, detail="body is required for a free-text message.")
        if len(text_body) > MAX_BODY_LEN:
            raise HTTPException(status_code=422, detail=f"body too long (max {MAX_BODY_LEN} chars).")
        template_code, params = None, {}

    try:
        with service_session() as conn:
            row = conn.execute(
                text(f"""
                    SELECT {_COLS} FROM marketplace.send_institution_message(
                        CAST(:rid AS uuid), CAST(:iid AS uuid), CAST(:mid AS uuid),
                        :tpl, CAST(:params AS jsonb), :body)
                """),
                {"rid": str(request_id), "iid": inst_id, "mid": str(member_id),
                 "tpl": template_code, "params": json.dumps(params), "body": text_body},
            ).one()
    except DBAPIError as exc:
        msg = str(getattr(exc, "orig", exc))
        hit = next((v for k, v in _ERRORS.items() if k in msg), None)
        if hit:
            raise HTTPException(status_code=hit[0], detail=hit[1]) from exc
        raise
    return _mask(row)
