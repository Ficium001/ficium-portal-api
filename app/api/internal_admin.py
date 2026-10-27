"""
Admin command channel: the ONLY way the portal admin site reads or changes portal data.

The admin site has its own database and never connects to this one. It calls two
endpoints here, both POST with a JSON body:

    POST /internal/admin/v1/query     read-only ops (one row set per call)
    POST /internal/admin/v1/commands  changes; idempotent by command_id

Every call must pass BOTH checks, so neither a leaked signing key nor a stolen
staff token is enough on its own:

  1. Ficium-Signature: HMAC-SHA256 over the raw body (same scheme and 300 s window
     as the integration contract), with a key only the admin site's backend holds.
     The op is inside the signed body, so a captured call cannot be re-aimed.
  2. X-Ficium-Actor: the acting staff member's ficium-auth access token. Must be
     from the Ficium platform institution, a staff role, and signed in WITH MFA.

Four-eyes is decided in the admin database; this side re-checks what it can see:
for four-eyes ops the maker must differ from the actor (the approver). Every
command writes an audit.event and an append-only integration.admin_command_log
row; replaying a command_id returns the stored result and changes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import ficium_contract as fc
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.db import service_session
from ..core.security import TokenError, verify_token

log = structlog.get_logger()
router = APIRouter(prefix="/internal/admin/v1", tags=["internal-admin"], include_in_schema=False)

MAX_BODY = 64_000
ACTOR_HEADER = "X-Ficium-Actor"
FOUR_EYES_OPS = frozenset({"waiver.grant", "waiver.revoke"})
MAX_WAIVER_DAYS = 180


class ChannelError(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status, self.code, self.detail = status, code, detail


def _err(status: int, code: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body)


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def _uuid(v: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, TypeError) as e:
        raise ChannelError(422, "invalid_field", f"{field} must be a UUID") from e


def _reason(v: Any, minimum: int = 10) -> str:
    r = str(v or "").strip()
    if not minimum <= len(r) <= 500:
        raise ChannelError(422, "invalid_field", f"reason must be {minimum}-500 characters")
    return r


# ── Authentication ────────────────────────────────────────────────────────────

async def _authenticate(request: Request) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    keys = settings.admin_command_keys
    if not keys:
        raise ChannelError(503, "admin_channel_disabled")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ChannelError(413, "body_too_large")
    try:
        fc.verify(request.headers.get(fc.SIGNATURE_HEADER, ""), raw, keys)
    except fc.ContractError as e:
        log.warning("admin_channel_bad_signature", reason=str(e))
        raise ChannelError(401, "bad_signature") from e

    token = request.headers.get(ACTOR_HEADER, "")
    if not token:
        raise ChannelError(401, "actor_required")
    try:
        claims = await verify_token(token)
    except TokenError as e:
        log.warning("admin_channel_bad_actor", reason=str(e))
        raise ChannelError(401, "bad_actor") from e
    if str(claims.get("institution_id")) != settings.platform_institution_id:
        raise ChannelError(403, "not_staff")
    if str(claims.get("user_role", "")).lower() not in settings.admin_staff_role_set:
        raise ChannelError(403, "not_staff")
    amr = claims.get("amr")
    if not isinstance(amr, list) or "mfa" not in amr:
        raise ChannelError(403, "mfa_required")
    claims["sub"] = _uuid(claims.get("sub"), "actor sub")

    try:
        body = json.loads(raw)
    except ValueError as e:
        raise ChannelError(422, "invalid_json") from e
    if not isinstance(body, dict) or not isinstance(body.get("op"), str):
        raise ChannelError(422, "op_required")
    args = body.get("args", {})
    if not isinstance(args, dict):
        raise ChannelError(422, "invalid_field", "args must be an object")
    body["args"] = args
    return raw, body, claims


def _audit(conn: Session, actor: str, action: str, resource_type: str, resource_id: str | None,
           institution_id: str | None, meta: dict[str, Any], outcome: str = "success") -> None:
    conn.execute(
        text("""
            INSERT INTO audit.event (actor_id, actor_type, institution_id, action,
                                     resource_type, resource_id, outcome, metadata)
            VALUES (CAST(:actor AS uuid), 'admin', CAST(:iid AS uuid), :action,
                    :rtype, CAST(:rid AS uuid), :outcome, CAST(:meta AS jsonb))
        """),
        {"actor": actor, "iid": institution_id, "action": action, "rtype": resource_type,
         "rid": resource_id, "outcome": outcome,
         "meta": json.dumps({**meta, "via": "admin-portal"}, default=str)},
    )


# ── Queries ───────────────────────────────────────────────────────────────────

def _q_compliance_overview(conn: Session, args: dict, actor: str) -> list[dict]:
    rows = conn.execute(text("""
        SELECT i.id AS institution_id, i.name, i.institution_type, i.onboarding_stage,
               c.is_approved, c.is_compliant, c.can_bid, c.missing_docs, c.waived_until,
               w.reason AS waiver_reason, w.granted_at AS waiver_granted_at,
               w.revoked_at AS waiver_revoked_at,
               (SELECT count(*) FROM institution.doc d
                 WHERE d.institution_id = i.id AND d.status = 'pending') AS docs_pending
          FROM institution.institution i
          JOIN institution.compliance c ON c.institution_id = i.id
          LEFT JOIN institution.compliance_waiver w ON w.institution_id = i.id
         ORDER BY c.can_bid, i.name
         LIMIT 500
    """)).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["missing_docs"] = list(d["missing_docs"] or [])
        out.append(d)
    return out


def _q_documents_list(conn: Session, args: dict, actor: str) -> list[dict]:
    iid = _uuid(args["institution_id"], "institution_id") if args.get("institution_id") else None
    status = args.get("status")
    if status is not None and status not in ("pending", "approved", "rejected", "expired"):
        raise ChannelError(422, "invalid_field", "status is not valid")
    rows = conn.execute(text("""
        SELECT d.id, d.institution_id, i.name AS institution_name,
               dt.code AS doc_type_code, dt.label AS doc_type_label, dt.is_mandatory,
               d.file_name, d.mime_type, d.status, d.expiry_date, d.rejection_reason,
               d.uploaded_at, d.reviewed_at, d.reviewed_by_staff
          FROM institution.doc d
          JOIN institution.institution i ON i.id = d.institution_id
          JOIN institution.doc_type dt   ON dt.id = d.doc_type_id
         WHERE (CAST(:iid AS uuid) IS NULL OR d.institution_id = CAST(:iid AS uuid))
           AND (CAST(:status AS text) IS NULL OR d.status = :status)
         ORDER BY CASE d.status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,
                  d.uploaded_at DESC
         LIMIT 500
    """), {"iid": iid, "status": status}).fetchall()
    return [dict(r._mapping) for r in rows]


async def _q_document_download(conn: Session, args: dict, actor: str) -> dict:
    from ..core.storage import storage_signed_url

    doc_id = _uuid(args.get("doc_id"), "doc_id")
    row = conn.execute(text(
        "SELECT institution_id, storage_path, file_name FROM institution.doc WHERE id = CAST(:id AS uuid)"
    ), {"id": doc_id}).fetchone()
    if row is None or not row.storage_path:
        raise ChannelError(404, "not_found", "Document not found.")
    # Viewing a bank's compliance document is itself recorded.
    _audit(conn, actor, "compliance.document_viewed", "institution_doc", doc_id,
           str(row.institution_id), {})
    return {"url": await storage_signed_url(row.storage_path, 300), "file_name": row.file_name}


SYNC_QUERIES: dict[str, Callable[[Session, dict, str], Any]] = {
    "compliance.overview": _q_compliance_overview,
    "documents.list": _q_documents_list,
}


@router.post("/query", response_model=None)
async def query(request: Request) -> JSONResponse:
    try:
        _, body, claims = await _authenticate(request)
        op = body["op"]
        with service_session() as conn:
            if op == "document.download":
                result = await _q_document_download(conn, body["args"], claims["sub"])
            elif op in SYNC_QUERIES:
                result = SYNC_QUERIES[op](conn, body["args"], claims["sub"])
            else:
                raise ChannelError(404, "unknown_op")
        return JSONResponse(status_code=200, content={"data": _jsonable(result)})
    except ChannelError as e:
        return _err(e.status, e.code, e.detail)


# ── Commands ──────────────────────────────────────────────────────────────────

def _compliance(conn: Session, iid: str) -> dict:
    row = conn.execute(text("""
        SELECT c.institution_id, c.can_bid, c.missing_docs, c.waived_until
          FROM institution.compliance c WHERE c.institution_id = CAST(:iid AS uuid)
    """), {"iid": iid}).fetchone()
    if row is None:
        raise ChannelError(404, "not_found", "Institution not found.")
    d = dict(row._mapping)
    d["missing_docs"] = list(d["missing_docs"] or [])
    return d


def _c_document_review(conn: Session, args: dict, ctx: dict) -> dict:
    doc_id = _uuid(args.get("doc_id"), "doc_id")
    decision = args.get("decision")
    if decision not in ("approve", "reject"):
        raise ChannelError(422, "invalid_field", "decision must be approve or reject")
    reason = _reason(args.get("rejection_reason"), 5) if decision == "reject" else None
    expiry = args.get("expiry_date")
    if expiry is not None:
        try:
            expiry = date.fromisoformat(str(expiry))
        except ValueError as e:
            raise ChannelError(422, "invalid_field", "expiry_date must be YYYY-MM-DD") from e
    row = conn.execute(text("""
        UPDATE institution.doc
           SET status = :status, rejection_reason = :reason,
               expiry_date = COALESCE(CAST(:expiry AS date), expiry_date),
               reviewed_by_staff = CAST(:by AS uuid), reviewed_at = now()
         WHERE id = CAST(:id AS uuid)
        RETURNING id, institution_id, status, rejection_reason, expiry_date, reviewed_at
    """), {"status": "approved" if decision == "approve" else "rejected", "reason": reason,
           "expiry": expiry, "by": ctx["checker"], "id": doc_id}).fetchone()
    if row is None:
        raise ChannelError(404, "not_found", "Document not found.")
    out = dict(row._mapping)
    _audit(conn, ctx["checker"], f"compliance.document_{out['status']}", "institution_doc", doc_id,
           str(out["institution_id"]),
           {"command_id": ctx["command_id"], "rejection_reason": reason})
    out["compliance"] = _compliance(conn, str(out["institution_id"]))
    return out


def _c_waiver_grant(conn: Session, args: dict, ctx: dict) -> dict:
    iid = _uuid(args.get("institution_id"), "institution_id")
    reason = _reason(args.get("reason"))
    try:
        until = datetime.fromisoformat(str(args.get("waived_until")).replace("Z", "+00:00"))
    except ValueError as e:
        raise ChannelError(422, "invalid_field", "waived_until must be an ISO timestamp") from e
    until = until if until.tzinfo else until.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if until <= now:
        raise ChannelError(422, "invalid_field", "The waiver must end in the future.")
    if until > now + timedelta(days=MAX_WAIVER_DAYS):
        raise ChannelError(422, "invalid_field", f"A waiver can last at most {MAX_WAIVER_DAYS} days.")
    _compliance(conn, iid)
    conn.execute(text("""
        INSERT INTO institution.compliance_waiver
          (institution_id, reason, waived_until, granted_by, granted_at)
        VALUES (CAST(:iid AS uuid), :reason, :until, CAST(:by AS uuid), now())
        ON CONFLICT (institution_id) DO UPDATE SET
          reason = excluded.reason, waived_until = excluded.waived_until,
          granted_by = excluded.granted_by, granted_at = now(),
          revoked_at = NULL, revoked_by = NULL
    """), {"iid": iid, "reason": reason, "until": until, "by": ctx["checker"]})
    _audit(conn, ctx["checker"], "compliance.waiver_granted", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "reason": reason,
            "waived_until": until.isoformat()})
    return _compliance(conn, iid)


def _c_waiver_revoke(conn: Session, args: dict, ctx: dict) -> dict:
    iid = _uuid(args.get("institution_id"), "institution_id")
    reason = _reason(args.get("reason"))
    hit = conn.execute(text("""
        UPDATE institution.compliance_waiver
           SET revoked_at = now(), revoked_by = CAST(:by AS uuid)
         WHERE institution_id = CAST(:iid AS uuid) AND revoked_at IS NULL
        RETURNING institution_id
    """), {"iid": iid, "by": ctx["checker"]}).fetchone()
    if hit is None:
        raise ChannelError(404, "not_found", "No active waiver for this institution.")
    _audit(conn, ctx["checker"], "compliance.waiver_revoked", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "reason": reason})
    return _compliance(conn, iid)


COMMANDS: dict[str, Callable[[Session, dict, dict], dict]] = {
    "document.review": _c_document_review,
    "waiver.grant": _c_waiver_grant,
    "waiver.revoke": _c_waiver_revoke,
}


def _run_command(raw: bytes, body: dict, actor: str) -> tuple[int, dict]:
    op = body["op"]
    if op not in COMMANDS:
        raise ChannelError(404, "unknown_op")
    command_id = _uuid(body.get("command_id"), "command_id")
    maker = _uuid(body.get("maker_sub"), "maker_sub")
    if op in FOUR_EYES_OPS and maker == actor:
        raise ChannelError(403, "four_eyes", "The person who asked for this cannot approve it.")
    if op not in FOUR_EYES_OPS and maker != actor:
        raise ChannelError(422, "invalid_field", "maker_sub must be the actor for this op")
    body_hash = hashlib.sha256(raw).hexdigest()

    with service_session() as conn:
        # Serialise per command_id so two deliveries of one command cannot both run.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": command_id})
        prev = conn.execute(text(
            "SELECT body_sha256, status_code, response FROM integration.admin_command_log "
            "WHERE command_id = CAST(:k AS uuid)"
        ), {"k": command_id}).fetchone()
        if prev is not None:
            if prev.body_sha256 != body_hash:
                raise ChannelError(409, "command_id_reused", "Same command_id, different body.")
            return int(prev.status_code), dict(prev.response)

        nested = conn.begin_nested()
        try:
            result = COMMANDS[op](conn, body["args"], {
                "command_id": command_id, "maker": maker, "checker": actor})
            nested.commit()
            status, payload = 200, {"data": _jsonable(result)}
        except ChannelError as e:
            nested.rollback()
            if e.status >= 500:
                raise
            # A refused command is recorded too, so a retry gets the same answer.
            status, payload = e.status, {"error": e.code, **({"detail": e.detail} if e.detail else {})}

        conn.execute(text("""
            INSERT INTO integration.admin_command_log
              (command_id, op, body_sha256, maker_sub, checker_sub, status_code, response)
            VALUES (CAST(:k AS uuid), :op, :h, CAST(:m AS uuid), CAST(:c AS uuid), :s, CAST(:r AS jsonb))
        """), {"k": command_id, "op": op, "h": body_hash, "m": maker, "c": actor,
               "s": status, "r": json.dumps(payload)})
        return status, payload


@router.post("/commands", response_model=None)
async def commands(request: Request) -> JSONResponse:
    try:
        raw, body, claims = await _authenticate(request)
        status, payload = await asyncio.to_thread(_run_command, raw, body, claims["sub"])
        return JSONResponse(status_code=status, content=payload)
    except ChannelError as e:
        return _err(e.status, e.code, e.detail)
