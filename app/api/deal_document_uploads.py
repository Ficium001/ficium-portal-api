"""Contract 1.6: the borrower answers a bank's document request.

    POST /integration/v1/deal-documents/uploads                       one-time upload slot
    PUT  /integration/v1/deal-documents/uploads/{upload_id}/content   the file's bytes (signed)
    POST /integration/v1/deal-documents/uploads/{upload_id}/complete  confirm with SHA-256

Architecture rule: everything between the borrower side and this side goes through
this interface API. The borrower side never sees a storage URL or the Portal DB;
this API stores the bytes itself.

Caller: the borrower app's server, signed with the same keys as the acceptance call.
OFF unless INTEGRATION_DOCUMENTS_ENABLED=true.

Only for a Phase 2 deal (accepted through the integration) whose request belongs to
the calling anon_borrower_id, and only for an active deal's borrower-provided item
that is still required or was rejected. A vault copy older than the item's
max_age_days is refused here too, not only by the borrower app.

The content endpoint checks the bytes (exact declared size, first bytes match the
declared type), stores them under the deal's folder and records their SHA-256.
complete compares that hash with the borrower's. Only then is the file registered with the same
deal_document_act('receive') bank staff use, marked uploaded_via='borrower'. The
existing review (verify / reject, four eyes) applies unchanged; the checklist change
is published back to the borrower by the db/031 triggers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Any

import ficium_contract as fc
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..core.config import settings
from ..core.db import service_session
from ..core.storage import storage_upload

log = structlog.get_logger()
router = APIRouter(prefix="/integration/v1/deal-documents", tags=["integration"])

SLOT_MINUTES = 15
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # contract: document_upload.request size_bytes maximum
_EXT = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}
_MAGIC = {"application/pdf": (b"%PDF-",), "image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",)}


def _e(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


async def _verified_body(request: Request, validate) -> dict[str, Any] | JSONResponse:
    keys = settings.integration_acceptance_keys
    if not settings.integration_documents_enabled or not keys:
        return _e(503, "documents_disabled")
    raw = await request.body()
    try:
        fc.verify(request.headers.get(fc.SIGNATURE_HEADER, ""), raw, keys)
    except fc.ContractError:
        return _e(401, "bad_signature")
    try:
        body = json.loads(raw)
        validate(body)
    except (ValueError, fc.ContractError) as e:
        return _e(422, "contract_violation", str(e)[:500])
    return body


def _owned_deal(conn: Any, pipeline_id: str, anon_borrower_id: str) -> Any:
    """The deal, if it is Phase 2 and its request belongs to this borrower; else None."""
    return conn.execute(text("""
        SELECT lp.id, lp.institution_id, lp.status
          FROM marketplace.loan_pipeline lp
          JOIN integration.request_shadow s ON s.request_id = lp.request_id
         WHERE lp.id = CAST(:pid AS uuid)
           AND s.anon_borrower_id = CAST(:anon AS uuid)
           AND integration.is_phase2(lp.id)
    """), {"pid": pipeline_id, "anon": anon_borrower_id}).fetchone()


def _item(conn: Any, pipeline_id: str, requirement_id: str) -> dict[str, Any] | None:
    payload = conn.execute(text("SELECT integration.build_deal_documents_payload(CAST(:p AS uuid))"),
                           {"p": pipeline_id}).scalar_one_or_none() or {}
    return next((d for d in payload.get("documents", []) if d["requirement_id"] == requirement_id), None)


def _open_slot(body: dict[str, Any]) -> tuple[JSONResponse | None, dict[str, Any] | None]:
    with service_session() as conn:
        deal = _owned_deal(conn, body["pipeline_id"], body["anon_borrower_id"])
        if deal is None:
            return _e(404, "deal_not_found"), None
        if deal.status != "active":
            return _e(409, "deal_not_active"), None
        doc = conn.execute(text("""
            SELECT dd.id, dd.status, r.provided_by, r.max_age_days
              FROM marketplace.deal_document dd
              JOIN institution.doc_requirement r ON r.id = dd.requirement_id
             WHERE dd.pipeline_id = CAST(:p AS uuid) AND dd.requirement_id = CAST(:r AS uuid)
        """), {"p": body["pipeline_id"], "r": body["requirement_id"]}).fetchone()
        if doc is None:
            return _e(404, "document_not_requested"), None
        if doc.provided_by != "borrower":
            return _e(409, "not_provided_by_borrower"), None
        if doc.status not in ("required", "rejected"):
            return _e(409, f"document_{doc.status}"), None
        doc_date = body.get("document_date")
        if body["source"] == "vault" and doc.max_age_days is not None and doc_date:
            # (the contract already requires document_date for a vault copy)
            if date.fromisoformat(str(doc_date)) < date.today() - timedelta(days=int(doc.max_age_days)):
                return _e(422, "document_too_old",
                          f"Older than {doc.max_age_days} days; upload a new one."), None
        path = (f"{deal.institution_id}/deals/{deal.id}/{doc.id}/"
                f"borrower-{uuid.uuid4()}.{_EXT[body['mime_type']]}")
        return None, {"deal": deal, "doc_id": str(doc.id), "path": path, "doc_date": doc_date}


@router.post("/uploads", response_model=None)
async def create_upload(request: Request) -> JSONResponse:
    body = await _verified_body(request, fc.validate_document_upload_request)
    if isinstance(body, JSONResponse):
        return body
    base = settings.integration_public_base_url.rstrip("/")
    if not base.startswith("https://"):
        return _e(503, "documents_not_configured", "INTEGRATION_PUBLIC_BASE_URL must be an https origin")
    err, ctx = await asyncio.to_thread(_open_slot, body)
    if err is not None:
        return err
    assert ctx is not None

    def _record() -> dict[str, Any]:
        with service_session() as conn:
            row = conn.execute(text("""
                INSERT INTO integration.document_upload
                    (pipeline_id, deal_document_id, requirement_id, institution_id, storage_path,
                     file_name, mime_type, size_bytes, source, document_date, expires_at)
                VALUES (CAST(:p AS uuid), CAST(:d AS uuid), CAST(:r AS uuid), CAST(:i AS uuid), :path,
                        :fname, :mime, :size, :src, :ddate, now() + make_interval(mins => :mins))
                RETURNING upload_id, expires_at
            """), {"p": body["pipeline_id"], "d": ctx["doc_id"], "r": body["requirement_id"],
                   "i": str(ctx["deal"].institution_id), "path": ctx["path"],
                   "fname": body["file_name"], "mime": body["mime_type"], "size": body["size_bytes"],
                   "src": body["source"], "ddate": ctx["doc_date"], "mins": SLOT_MINUTES}).one()
            conn.commit()
            return {"upload_id": str(row.upload_id),
                    "upload_url": f"{base}/integration/v1/deal-documents/uploads/{row.upload_id}/content",
                    "expires_at": row.expires_at.isoformat().replace("+00:00", "Z")}

    out = await asyncio.to_thread(_record)
    fc.validate_document_upload_response(out)
    return JSONResponse(status_code=201, content=out)


@router.put("/uploads/{upload_id}/content", response_model=None)
async def put_content(upload_id: str, request: Request) -> JSONResponse:
    keys = settings.integration_acceptance_keys
    if not settings.integration_documents_enabled or not keys:
        return _e(503, "documents_disabled")
    try:
        uuid.UUID(upload_id)
    except ValueError:
        return _e(404, "upload_not_found")
    # Refuse oversized bodies before reading them: the signature can only be
    # checked after the bytes are in memory.
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        return _e(411, "content_length_required")
    if declared > MAX_UPLOAD_BYTES:
        return _e(413, "too_large")
    raw = await request.body()
    if len(raw) > MAX_UPLOAD_BYTES:
        return _e(413, "too_large")
    try:
        fc.verify(request.headers.get(fc.SIGNATURE_HEADER, ""), raw, keys)
    except fc.ContractError:
        return _e(401, "bad_signature")

    def _load() -> Any:
        with service_session() as conn:
            return conn.execute(text("""
                SELECT *, expires_at < now() AS expired FROM integration.document_upload
                 WHERE upload_id = CAST(:u AS uuid)
            """), {"u": upload_id}).fetchone()

    up = await asyncio.to_thread(_load)
    if up is None:
        return _e(404, "upload_not_found")
    if up.status != "open":
        return _e(409, "upload_already_completed")
    if up.expired:
        return _e(410, "upload_expired")
    if request.headers.get("content-type", "").split(";")[0].strip() != up.mime_type:
        return _e(415, "content_type_mismatch")
    problem = _check_file(raw, up)
    if problem:
        log.warning("borrower_upload_rejected", upload_id=upload_id, reason=problem)
        return _e(422, problem)
    await storage_upload(up.storage_path, raw, up.mime_type)

    def _mark() -> None:
        with service_session() as conn:
            conn.execute(text("""
                UPDATE integration.document_upload
                   SET content_sha256 = :sha, content_received_at = now()
                 WHERE upload_id = CAST(:u AS uuid) AND status = 'open'
            """), {"sha": hashlib.sha256(raw).hexdigest(), "u": upload_id})
            conn.commit()

    await asyncio.to_thread(_mark)
    return JSONResponse(status_code=200, content={"upload_id": upload_id, "size_bytes": len(raw)})


def _check_file(data: bytes, up: Any) -> str | None:
    if len(data) != up.size_bytes:
        return "size_mismatch"
    if not data.startswith(_MAGIC[up.mime_type]):
        return "type_mismatch"
    return None


@router.post("/uploads/{upload_id}/complete", response_model=None)
async def complete_upload(upload_id: str, request: Request) -> JSONResponse:
    body = await _verified_body(request, fc.validate_document_complete_request)
    if isinstance(body, JSONResponse):
        return body
    if body["upload_id"] != upload_id:
        return _e(422, "upload_id_mismatch")

    def _load() -> Any:
        with service_session() as conn:
            if _owned_deal(conn, body["pipeline_id"], body["anon_borrower_id"]) is None:
                return None
            return conn.execute(text("""
                SELECT * FROM integration.document_upload
                 WHERE upload_id = CAST(:u AS uuid) AND pipeline_id = CAST(:p AS uuid)
            """), {"u": upload_id, "p": body["pipeline_id"]}).fetchone()

    up = await asyncio.to_thread(_load)
    if up is None:
        return _e(404, "upload_not_found")
    if up.status != "open":
        return _e(409, "upload_already_completed")
    if up.content_sha256 is None:
        return _e(409, "content_not_received")
    if up.content_sha256 != body["sha256"]:
        log.warning("borrower_upload_rejected", upload_id=upload_id, reason="hash_mismatch")
        return _e(422, "hash_mismatch")

    def _register() -> tuple[JSONResponse | None, dict[str, Any] | None]:
        with service_session() as conn:
            locked = conn.execute(text("""
                SELECT status, expires_at < now() AS expired FROM integration.document_upload
                 WHERE upload_id = CAST(:u AS uuid) FOR UPDATE
            """), {"u": upload_id}).one()
            if locked.status != "open":
                return _e(409, "upload_already_completed"), None
            if locked.expired:
                return _e(410, "upload_expired"), None
            try:
                conn.execute(text("""
                    SELECT marketplace.deal_document_act(
                        CAST(:d AS uuid), CAST(:i AS uuid), 'receive', NULL, NULL,
                        :path, :fname, :mime, :size)
                """), {"d": str(up.deal_document_id), "i": str(up.institution_id), "path": up.storage_path,
                       "fname": up.file_name[:200], "mime": up.mime_type, "size": up.size_bytes})
            except Exception as exc:  # noqa: BLE001 - mapped to the contract's error shape
                conn.rollback()
                msg = str(exc)
                code = next((c for c in ("DOC_DEAL_CLOSED", "DOC_ALREADY_VERIFIED") if c in msg), None)
                if code:
                    return _e(409, code.lower()), None
                raise
            conn.execute(text("""
                UPDATE marketplace.deal_document
                   SET uploaded_via = 'borrower', source = :src, document_date = :ddate
                 WHERE id = CAST(:d AS uuid)
            """), {"src": up.source, "ddate": up.document_date, "d": str(up.deal_document_id)})
            conn.execute(text("""
                UPDATE integration.document_upload
                   SET status = 'completed', sha256 = :sha, completed_at = now()
                 WHERE upload_id = CAST(:u AS uuid)
            """), {"sha": body["sha256"], "u": upload_id})
            conn.execute(text("""
                INSERT INTO audit.event
                    (id, occurred_at, actor_id, actor_type, institution_id, action,
                     resource_type, resource_id, outcome, metadata)
                VALUES (gen_random_uuid(), now(), NULL, 'consumer', CAST(:i AS uuid),
                        'deal_document.receive', 'deal_document', CAST(:d AS uuid), 'success',
                        CAST(:meta AS jsonb))
            """), {"i": str(up.institution_id), "d": str(up.deal_document_id),
                   "meta": json.dumps({"pipeline_id": str(up.pipeline_id), "via": "borrower",
                                       "source": up.source, "sha256": body["sha256"],
                                       "document_date": str(up.document_date) if up.document_date else None})})
            item = _item(conn, str(up.pipeline_id), str(up.requirement_id))
            conn.commit()
            return None, item

    err, item = await asyncio.to_thread(_register)
    if err is not None:
        return err
    out = {"pipeline_id": str(up.pipeline_id), "document": item}
    fc.validate_document_complete_response(out)
    return JSONResponse(status_code=200, content=out)
