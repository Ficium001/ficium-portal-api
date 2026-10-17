# =============================================================================
# ficium-portal-api — Document checklist (db/027)
#
# Deal side (inst:pipeline):
#   GET  /pipelines/{id}/documents                       checklist for the deal
#   POST /pipelines/{id}/documents/{doc}/upload-url      signed upload URL
#   POST /pipelines/{id}/documents/{doc}/receive         register the uploaded file
#   POST /pipelines/{id}/documents/{doc}/verify          second person confirms it
#   POST /pipelines/{id}/documents/{doc}/reject          send back, with a note
#   POST /pipelines/{id}/documents/{doc}/waive           not needed, with a reason
#   GET  /pipelines/{id}/documents/{doc}/file            signed download URL
#
# Configuration (inst:settings):
#   GET|POST       /pipelines/templates/{tid}/documents
#   PUT|DELETE     /pipelines/templates/{tid}/documents/{rid}
#
# Rules (four eyes, notes, closed deals, no replacing verified files) live in
# marketplace.deal_document_act(); the stage gate in
# marketplace.stage_documents_outstanding(), enforced by the advance endpoints.
# =============================================================================
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import service_session
from ..core.storage import storage_signed_upload_url, storage_signed_url
from ..deps import current_claims, require_module

router = APIRouter(tags=["documents-checklist"], dependencies=[Depends(require_module("pipeline"))])

MAX_BYTES = 20 * 1024 * 1024
ALLOWED_MIME = {
    "application/pdf", "image/jpeg", "image/png", "image/heic", "image/webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_ERRORS: dict[str, tuple[int, str]] = {
    "DOC_NOT_FOUND": (404, "Document not found."),
    "DOC_DEAL_CLOSED": (409, "This deal is closed; its documents can't change."),
    "DOC_ALREADY_VERIFIED": (409, "This document is verified and can't be replaced."),
    "DOC_FILE_REQUIRED": (422, "Upload a file first."),
    "DOC_NOT_RECEIVED": (409, "Only a received document can be verified or rejected."),
    "DOC_FOUR_EYES": (403, "Someone other than the person who uploaded it must verify it."),
    "DOC_NOTE_REQUIRED": (422, "Add a note explaining why."),
    "DOC_NOT_WAIVABLE": (409, "This document is already verified or waived."),
    "DOC_ACTION_INVALID": (422, "Unknown action."),
}


def _ctx(claims: dict[str, Any]) -> tuple[str, str]:
    iid, uid = claims.get("institution_id"), claims.get("sub")
    if not iid or not uid:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    return str(iid), str(uid)


def _modules(conn: Session, uid: str) -> list[str]:
    row = conn.execute(text("""
        SELECT COALESCE(cg.module_permissions, pg.module_permissions, '{}'::text[]) AS modules
          FROM institution.member m
          LEFT JOIN institution."group"      cg ON cg.id = m.custom_group_id
          LEFT JOIN portal_admin.user_groups pg ON pg.id = m.group_id
         WHERE m.auth_user_id = CAST(:uid AS uuid) AND m.active
         LIMIT 1
    """), {"uid": uid}).first()
    return list(row.modules) if row and row.modules else []


def _require(conn: Session, claims: dict[str, Any], key: str) -> None:
    if claims.get("user_role") in ("super_admin", "institution_super_admin"):
        return
    mods = _modules(conn, str(claims.get("sub")))
    if "*" not in mods and key not in mods:
        raise HTTPException(status_code=403, detail=f"Requires '{key}'.")


def _deal(conn: Session, iid: str, pid: str) -> None:
    ok = conn.execute(text("""
        SELECT 1 FROM marketplace.loan_pipeline
         WHERE id = CAST(:pid AS uuid) AND institution_id = CAST(:iid AS uuid)
    """), {"pid": pid, "iid": iid}).first()
    if not ok:
        raise HTTPException(status_code=404, detail="Deal not found.")


def _doc(conn: Session, iid: str, pid: str, doc_id: str) -> dict[str, Any]:
    row = conn.execute(text("""
        SELECT id, status, storage_path, file_name FROM marketplace.deal_document
         WHERE id = CAST(:d AS uuid) AND pipeline_id = CAST(:pid AS uuid)
           AND institution_id = CAST(:iid AS uuid)
    """), {"d": doc_id, "pid": pid, "iid": iid}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found.")
    return dict(row)


def _prefix(iid: str, pid: str, doc_id: str) -> str:
    return f"{iid}/deals/{pid}/{doc_id}/"


# ── Deal checklist ────────────────────────────────────────────────────────────
@router.get("/pipelines/{pipeline_id}/documents")
def list_deal_documents(pipeline_id: str, claims: dict = Depends(current_claims)) -> list[dict]:
    iid, _ = _ctx(claims)
    with service_session() as conn:
        _deal(conn, iid, pipeline_id)
        conn.execute(text("SELECT marketplace.ensure_deal_documents(CAST(:p AS uuid))"),
                     {"p": pipeline_id})
        conn.commit()
        rows = conn.execute(text("""
            SELECT dd.id, dd.status, dd.file_name, dd.mime_type, dd.file_size,
                   dd.uploaded_at, dd.reviewed_at, dd.review_note,
                   dd.uploaded_by, dd.reviewed_by,
                   portal_admin.get_user_display_name(dd.uploaded_by) AS uploaded_by_name,
                   portal_admin.get_user_display_name(dd.reviewed_by) AS reviewed_by_name,
                   r.id AS requirement_id, r.name, r.description, r.provided_by, r.mandatory,
                   r.position, r.stage_def_id,
                   sd.label AS stage_label, sd.position AS stage_position,
                   psi.status AS stage_status
              FROM marketplace.deal_document dd
              JOIN institution.doc_requirement r ON r.id = dd.requirement_id
              LEFT JOIN institution.pipeline_stage_def sd ON sd.id = r.stage_def_id
              LEFT JOIN marketplace.pipeline_stage_instance psi
                     ON psi.pipeline_id = dd.pipeline_id AND psi.stage_def_id = r.stage_def_id
             WHERE dd.pipeline_id = CAST(:p AS uuid) AND dd.institution_id = CAST(:iid AS uuid)
               AND (r.is_active OR dd.status <> 'required')
             ORDER BY sd.position NULLS LAST, r.position, r.name
        """), {"p": pipeline_id, "iid": iid}).mappings().all()
    return [dict(r) for r in rows]


@router.post("/pipelines/{pipeline_id}/documents/{doc_id}/upload-url")
async def deal_document_upload_url(
    pipeline_id: str, doc_id: str, body: dict = Body(...), claims: dict = Depends(current_claims),
) -> dict:
    iid, _ = _ctx(claims)
    mime = str(body.get("mime_type") or "")
    size = int(body.get("file_size") or 0)
    if mime not in ALLOWED_MIME:
        raise HTTPException(status_code=422, detail="Upload a PDF, image, Word or Excel file.")
    if size <= 0 or size > MAX_BYTES:
        raise HTTPException(status_code=422, detail="Files must be under 20 MB.")
    with service_session() as conn:
        doc = _doc(conn, iid, pipeline_id, doc_id)
    if doc["status"] == "verified":
        raise HTTPException(status_code=409, detail=_ERRORS["DOC_ALREADY_VERIFIED"][1])
    name = str(body.get("file_name") or "file")
    ext = name.rsplit(".", 1)[-1].lower()[:8] if "." in name else "bin"
    return await storage_signed_upload_url(f"{_prefix(iid, pipeline_id, doc_id)}{uuid.uuid4()}.{ext}")


def _act(pipeline_id: str, doc_id: str, claims: dict, action: str, body: dict) -> dict:
    iid, uid = _ctx(claims)
    with service_session() as conn:
        _doc(conn, iid, pipeline_id, doc_id)
        if action == "receive":
            path = str(body.get("storage_path") or "")
            if not path.startswith(_prefix(iid, pipeline_id, doc_id)):
                raise HTTPException(status_code=422, detail="That file doesn't belong to this document.")
        try:
            result = conn.execute(text("""
                SELECT marketplace.deal_document_act(
                    CAST(:d AS uuid), CAST(:iid AS uuid), :action, CAST(:uid AS uuid), :note,
                    :path, :fname, :mime, :size) AS r
            """), {
                "d": doc_id, "iid": iid, "action": action, "uid": uid,
                "note": body.get("note"), "path": body.get("storage_path"),
                "fname": (str(body["file_name"])[:200] if body.get("file_name") else None),
                "mime": body.get("mime_type"), "size": body.get("file_size"),
            }).scalar_one()
        except Exception as exc:  # noqa: BLE001 — mapped below
            conn.rollback()
            for code, (status, detail) in _ERRORS.items():
                if code in str(exc):
                    raise HTTPException(status_code=status, detail=detail) from exc
            raise
        conn.execute(text("""
            INSERT INTO audit.event
                (id, occurred_at, actor_id, actor_type, actor_email, actor_role, institution_id,
                 action, resource_type, resource_id, outcome, metadata)
            VALUES (gen_random_uuid(), now(), CAST(:uid AS uuid), 'member', :email, :role,
                    CAST(:iid AS uuid), :action, 'deal_document', CAST(:d AS uuid), 'success',
                    CAST(:meta AS jsonb))
        """), {"uid": uid, "email": claims.get("email"), "role": claims.get("user_role"),
               "iid": iid, "action": f"deal_document.{action}", "d": doc_id,
               "meta": json.dumps({"pipeline_id": pipeline_id, "note": body.get("note"),
                                   "file_name": body.get("file_name")})})
        conn.commit()
    return dict(result)


@router.post("/pipelines/{pipeline_id}/documents/{doc_id}/receive")
def receive_document(pipeline_id: str, doc_id: str, body: dict = Body(...),
                     claims: dict = Depends(current_claims)) -> dict:
    return _act(pipeline_id, doc_id, claims, "receive", body)


@router.post("/pipelines/{pipeline_id}/documents/{doc_id}/verify")
def verify_document(pipeline_id: str, doc_id: str, body: dict = Body(default={}),
                    claims: dict = Depends(current_claims)) -> dict:
    return _act(pipeline_id, doc_id, claims, "verify", body)


@router.post("/pipelines/{pipeline_id}/documents/{doc_id}/reject")
def reject_document(pipeline_id: str, doc_id: str, body: dict = Body(...),
                    claims: dict = Depends(current_claims)) -> dict:
    return _act(pipeline_id, doc_id, claims, "reject", body)


@router.post("/pipelines/{pipeline_id}/documents/{doc_id}/waive")
def waive_document(pipeline_id: str, doc_id: str, body: dict = Body(...),
                   claims: dict = Depends(current_claims)) -> dict:
    return _act(pipeline_id, doc_id, claims, "waive", body)


@router.get("/pipelines/{pipeline_id}/documents/{doc_id}/file")
async def document_file(pipeline_id: str, doc_id: str,
                        claims: dict = Depends(current_claims)) -> dict:
    iid, _ = _ctx(claims)
    with service_session() as conn:
        doc = _doc(conn, iid, pipeline_id, doc_id)
    if not doc["storage_path"]:
        raise HTTPException(status_code=404, detail="No file uploaded yet.")
    return {"url": await storage_signed_url(doc["storage_path"], 300), "file_name": doc["file_name"]}


# ── Checklist configuration (per workflow) ────────────────────────────────────
_REQ_COLS = "id, template_id, stage_def_id, name, description, provided_by, mandatory, position, is_active"


def _template(conn: Session, iid: str, tid: str) -> None:
    if not conn.execute(text("""
        SELECT 1 FROM institution.pipeline_template
         WHERE id = CAST(:t AS uuid) AND institution_id = CAST(:iid AS uuid)
    """), {"t": tid, "iid": iid}).first():
        raise HTTPException(status_code=404, detail="Workflow not found.")


def _clean(body: dict, conn: Session, tid: str, partial: bool) -> dict:
    out: dict[str, Any] = {}
    if "name" in body or not partial:
        name = str(body.get("name") or "").strip()
        if not 1 <= len(name) <= 120:
            raise HTTPException(status_code=422, detail="Name the document (1 to 120 characters).")
        out["name"] = name
    if "description" in body:
        out["description"] = (str(body["description"]).strip() or None) if body["description"] else None
    if "provided_by" in body or not partial:
        pb = body.get("provided_by", "borrower")
        if pb not in ("borrower", "bank", "third_party"):
            raise HTTPException(status_code=422, detail="Provided by must be borrower, bank or third party.")
        out["provided_by"] = pb
    if "mandatory" in body or not partial:
        out["mandatory"] = bool(body.get("mandatory", True))
    if "position" in body:
        out["position"] = int(body["position"])
    if "stage_def_id" in body or not partial:
        sid = body.get("stage_def_id")
        if sid and not conn.execute(text("""
            SELECT 1 FROM institution.pipeline_stage_def
             WHERE id = CAST(:s AS uuid) AND template_id = CAST(:t AS uuid)
        """), {"s": sid, "t": tid}).first():
            raise HTTPException(status_code=422, detail="That stage isn't part of this workflow.")
        out["stage_def_id"] = sid or None
    return out


@router.get("/pipelines/templates/{template_id}/documents")
def list_requirements(template_id: str, claims: dict = Depends(current_claims)) -> list[dict]:
    iid, _ = _ctx(claims)
    with service_session() as conn:
        _template(conn, iid, template_id)
        rows = conn.execute(text(f"""
            SELECT {_REQ_COLS} FROM institution.doc_requirement
             WHERE template_id = CAST(:t AS uuid) AND institution_id = CAST(:iid AS uuid) AND is_active
             ORDER BY position, name
        """), {"t": template_id, "iid": iid}).mappings().all()
    return [dict(r) for r in rows]


@router.post("/pipelines/templates/{template_id}/documents", status_code=201)
def create_requirement(template_id: str, body: dict = Body(...),
                       claims: dict = Depends(current_claims)) -> dict:
    iid, _ = _ctx(claims)
    with service_session() as conn:
        _require(conn, claims, "inst:settings")
        _template(conn, iid, template_id)
        v = _clean(body, conn, template_id, partial=False)
        row = conn.execute(text(f"""
            INSERT INTO institution.doc_requirement
                (institution_id, template_id, stage_def_id, name, description, provided_by, mandatory, position)
            VALUES (CAST(:iid AS uuid), CAST(:t AS uuid), CAST(:stage_def_id AS uuid), :name, :description,
                    :provided_by, :mandatory,
                    COALESCE(:position, (SELECT COALESCE(max(position), 0) + 1 FROM institution.doc_requirement
                                          WHERE template_id = CAST(:t AS uuid))))
            RETURNING {_REQ_COLS}
        """), {"iid": iid, "t": template_id, "description": None, "position": None, **v}).mappings().one()
        conn.commit()
    return dict(row)


@router.put("/pipelines/templates/{template_id}/documents/{req_id}")
def update_requirement(template_id: str, req_id: str, body: dict = Body(...),
                       claims: dict = Depends(current_claims)) -> dict:
    iid, _ = _ctx(claims)
    with service_session() as conn:
        _require(conn, claims, "inst:settings")
        _template(conn, iid, template_id)
        v = _clean(body, conn, template_id, partial=True)
        if not v:
            raise HTTPException(status_code=422, detail="Nothing to change.")
        sets = ", ".join(
            f"{k} = CAST(:{k} AS uuid)" if k == "stage_def_id" else f"{k} = :{k}" for k in v
        )
        row = conn.execute(text(f"""
            UPDATE institution.doc_requirement SET {sets}, updated_at = now()
             WHERE id = CAST(:rid AS uuid) AND template_id = CAST(:t AS uuid)
               AND institution_id = CAST(:iid AS uuid)
            RETURNING {_REQ_COLS}
        """), {"rid": req_id, "t": template_id, "iid": iid, **v}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Document requirement not found.")
        conn.commit()
    return dict(row)


@router.delete("/pipelines/templates/{template_id}/documents/{req_id}", status_code=204)
def remove_requirement(template_id: str, req_id: str, claims: dict = Depends(current_claims)) -> None:
    """Soft delete: deals that already collected this document keep it."""
    iid, _ = _ctx(claims)
    with service_session() as conn:
        _require(conn, claims, "inst:settings")
        res = conn.execute(text("""
            UPDATE institution.doc_requirement SET is_active = false, updated_at = now()
             WHERE id = CAST(:rid AS uuid) AND template_id = CAST(:t AS uuid)
               AND institution_id = CAST(:iid AS uuid)
        """), {"rid": req_id, "t": template_id, "iid": iid})
        if getattr(res, "rowcount", 1) == 0:
            raise HTTPException(status_code=404, detail="Document requirement not found.")
        conn.commit()
