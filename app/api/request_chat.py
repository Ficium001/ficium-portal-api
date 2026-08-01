# =============================================================================
# Request chat — institution side
#
# Chat lives in the App DB (public.request_messages), NOT the Portal DB. The
# portal frontend therefore cannot reach it with a Supabase client at all —
# every route here proxies through app_service_session(). That mismatch is why
# the portal's chat component sat as a stub: it was wired to a Portal DB client
# that could never see the table.
#
# Two invariants this module exists to hold:
#
#   1. Thread isolation. A message belongs to (request_id, institution_id).
#      Every query below filters on the CALLER's institution_id from their JWT,
#      never on a value supplied by the client, so one bank cannot read another
#      bank's conversation on the same request.
#
#   2. Borrower anonymity. request_messages.sender_id is the borrower's real
#      auth.uid() — a stable identifier that would link the same person across
#      every request they have ever posted. It is NEVER projected outward.
#      _mask() is the only shape that leaves this module.
#
# The App DB enforces the rest (structured-only before acceptance, free text
# for the winner only, frozen losing threads) in a BEFORE trigger, which
# matters here because a service session bypasses RLS entirely.
# =============================================================================
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text

from ..core.db import AppDatabaseUnavailable, app_service_session
from ..deps import current_claims

router = APIRouter(prefix="/marketplace/requests", tags=["marketplace"])

MAX_BODY_LEN = 2000


def _institution_id(claims: dict) -> str:
    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")
    return str(inst_id)


def _mask(row) -> dict:
    """Project a message for institution eyes.

    Deliberately omits sender_id. The borrower's auth.uid() is stable across
    every request they post, so returning it would let an institution correlate
    the same anonymous borrower across the marketplace — exactly what the
    pre-acceptance anonymity model prevents everywhere else.
    """
    m = row._mapping
    return {
        "id": str(m["id"]),
        "request_id": str(m["request_id"]),
        "sender_type": m["sender_type"],
        "sender_label": "You" if m["sender_type"] == "institution" else "Borrower",
        "body": m["body"],
        "kind": m["kind"],
        "template_code": m["template_code"],
        "params": m["params"],
        "created_at": m["created_at"],
    }


def _thread_state(app_conn, request_id: str, institution_id: str) -> dict:
    """Whether this institution may still write, and in what mode."""
    row = app_conn.execute(
        text("""
            SELECT public.request_chat_is_open(CAST(:rid AS uuid), CAST(:iid AS uuid))   AS is_open,
                   public.request_chat_is_winner(CAST(:rid AS uuid), CAST(:iid AS uuid)) AS is_winner
        """),
        {"rid": request_id, "iid": institution_id},
    ).fetchone()
    return {
        "is_open": bool(row.is_open),
        "is_winner": bool(row.is_winner),
        # Free text only once this institution has actually won; before that the
        # marketplace is still anonymous and both sides are template-bound.
        "can_send_free_text": bool(row.is_winner),
    }


@router.get("/message-templates")
async def list_message_templates(
    claims: dict = Depends(current_claims),
) -> list[dict]:
    """Structured messages this institution may send."""
    _institution_id(claims)
    try:
        with app_service_session() as app_conn:
            rows = app_conn.execute(
                text("""
                    SELECT code, label, body_template, params_schema, sort_order
                      FROM public.request_message_template
                     WHERE sender_type = 'institution' AND is_active
                     ORDER BY sort_order
                """)
            ).fetchall()
    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [dict(r._mapping) for r in rows]


@router.get("/{request_id}/messages")
async def list_messages(
    request_id: UUID,
    claims: dict = Depends(current_claims),
) -> dict:
    """This institution's thread on one request.

    Scoped to the caller's own institution_id from the JWT — a bank cannot pass
    someone else's id and read their conversation.
    """
    inst_id = _institution_id(claims)
    try:
        with app_service_session() as app_conn:
            rows = app_conn.execute(
                text("""
                    SELECT id, request_id, sender_type, body, kind,
                           template_code, params, created_at
                      FROM public.request_messages
                     WHERE request_id     = CAST(:rid AS uuid)
                       AND institution_id = CAST(:iid AS uuid)
                     ORDER BY created_at
                """),
                {"rid": str(request_id), "iid": inst_id},
            ).fetchall()
            state = _thread_state(app_conn, str(request_id), inst_id)
    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"messages": [_mask(r) for r in rows], **state}


@router.post("/{request_id}/messages", status_code=201)
async def send_message(
    request_id: UUID,
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    """Post a message onto this institution's thread.

    institution_id and sender_id come from the JWT, never the request body.
    Structured/free eligibility and thread-frozen state are enforced by the App
    DB trigger — a service session bypasses RLS, so the trigger is the real
    boundary and its errors are surfaced as 409 rather than swallowed.
    """
    inst_id = _institution_id(claims)
    member_id = claims.get("sub") or claims.get("member_id")
    if not member_id:
        raise HTTPException(status_code=403, detail="No member context.")

    kind = (body.get("kind") or "structured").strip()
    if kind not in ("structured", "free"):
        raise HTTPException(status_code=422, detail="kind must be 'structured' or 'free'.")

    template_code = body.get("template_code")
    params = body.get("params") or {}
    text_body = (body.get("body") or "").strip()

    if kind == "structured" and not template_code:
        raise HTTPException(status_code=422, detail="template_code is required for a structured message.")
    if kind == "free":
        if not text_body:
            raise HTTPException(status_code=422, detail="body is required for a free-text message.")
        if len(text_body) > MAX_BODY_LEN:
            raise HTTPException(status_code=422, detail=f"body too long (max {MAX_BODY_LEN} chars).")
        template_code = None

    try:
        with app_service_session() as app_conn:
            if kind == "structured":
                # Render server-side from the catalogue: the stored body must
                # match the template it claims, not whatever text a caller sends.
                tpl = app_conn.execute(
                    text("""
                        SELECT code, body_template FROM public.request_message_template
                         WHERE code = :code AND sender_type = 'institution' AND is_active
                    """),
                    {"code": template_code},
                ).fetchone()
                if tpl is None:
                    raise HTTPException(status_code=422, detail="Unknown or unavailable template.")
                text_body = tpl.body_template
                for key, value in params.items():
                    rendered = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
                    text_body = text_body.replace("{" + str(key) + "}", rendered)

            row = app_conn.execute(
                text("""
                    INSERT INTO public.request_messages
                        (request_id, institution_id, sender_type, sender_id,
                         body, kind, template_code, params)
                    VALUES
                        (CAST(:rid AS uuid), CAST(:iid AS uuid), 'institution', CAST(:sid AS uuid),
                         :body, :kind, :tpl, CAST(:params AS jsonb))
                    RETURNING id, request_id, sender_type, body, kind,
                              template_code, params, created_at
                """),
                {
                    "rid": str(request_id), "iid": inst_id, "sid": str(member_id),
                    "body": text_body, "kind": kind, "tpl": template_code,
                    "params": json.dumps(params),
                },
            ).fetchone()
    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # The App DB trigger rejects closed threads, ineligible free text and
        # cross-role templates. Surface its message rather than a blank 500.
        detail = str(getattr(exc, "orig", exc)).split("\n")[0]
        raise HTTPException(status_code=409, detail=detail) from exc

    return _mask(row)
