# =============================================================================
# ficium-portal-api — inst:doctemplates router
# Prefix: /institution/doc-templates
#
# Institution-authored .docx templates (loan agreement, facility letter,
# legal agreement) with maker-checker version publish, merge-field
# generation against a deal (loan_pipeline) snapshot, and .docx/.pdf output.
#
# Gate: institution-level licensing via require_module("inst:doctemplates")
# (institution.institution.modules) — same mechanism as the pipeline module,
# distinct from per-user group RBAC.
#
# Storage: Supabase Storage REST, same pattern as esign.py/documents.py
# (PORTAL_SUPABASE_URL + PORTAL_SUPABASE_SERVICE_KEY), bucket
# `institution-docs`, prefix `doc-templates/`.
# =============================================================================
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...core.db import service_session
from ...core.roles import INSTITUTION_ADMIN_ROLES
from ...deps import current_claims, require_module, tenant_conn
from . import merge_engine
from .schemas import (
    GenerateRequest,
    GenerationOut,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
    VersionCreate,
    VersionDecision,
    VersionOut,
)

MODULE_KEY = "inst:doctemplates"
_BUCKET = "institution-docs"
_PREFIX = "doc-templates"
ALLOWED_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

router = APIRouter(
    prefix="/institution/doc-templates",
    tags=["inst:doctemplates"],
    dependencies=[Depends(require_module(MODULE_KEY))],
)


def _institution_id(claims: dict) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    return iid


def _require_admin(claims: dict) -> None:
    if claims.get("user_role") not in INSTITUTION_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Requires institution admin.")


def _row(r) -> dict:
    return dict(r._mapping)


def _one(result) -> dict | None:
    row = result.fetchone()
    return dict(row._mapping) if row else None


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


def _audit(
    conn: Session, *, claims: dict, action: str, resource_type: str,
    resource_id, outcome: str = "success", metadata: dict | None = None,
) -> None:
    """Best-effort write to audit.event — never blocks the core action."""
    try:
        conn.execute(
            text("""
                INSERT INTO audit.event
                    (id, occurred_at, actor_id, actor_type, actor_email, actor_role,
                     institution_id, action, resource_type, resource_id, outcome, metadata)
                VALUES
                    (gen_random_uuid(), now(), CAST(:actor_id AS uuid), 'member', :actor_email, :actor_role,
                     CAST(:inst_id AS uuid), :action, :resource_type, CAST(:resource_id AS uuid),
                     :outcome, CAST(:metadata AS jsonb))
            """),
            {
                "actor_id": claims.get("sub"),
                "actor_email": claims.get("email"),
                "actor_role": claims.get("user_role"),
                "inst_id": _institution_id(claims),
                "action": action,
                "resource_type": resource_type,
                "resource_id": str(resource_id),
                "outcome": outcome,
                "metadata": json.dumps(metadata or {}),
            },
        )
    except Exception:
        pass  # audit is best-effort; core action already committed


# ── Supabase Storage helpers (same pattern as esign.py) ─────────────────────

async def _storage_upload(path: str, data: bytes, content_type: str) -> None:
    url = os.environ["PORTAL_SUPABASE_URL"]
    key = os.environ["PORTAL_SUPABASE_SERVICE_KEY"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/storage/v1/object/{_BUCKET}/{path}",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
            content=data,
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Could not store document.")


async def _storage_download(path: str) -> bytes:
    url = os.environ["PORTAL_SUPABASE_URL"]
    key = os.environ["PORTAL_SUPABASE_SERVICE_KEY"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{url}/storage/v1/object/{_BUCKET}/{path}",
            headers={"Authorization": f"Bearer {key}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch document.")
    return resp.content


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@router.get("", response_model=list[TemplateOut])
async def list_templates(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
):
    return _rows(conn.execute(
        text("""
            SELECT * FROM institution.doc_template
            WHERE institution_id = :iid
            ORDER BY created_at DESC
        """),
        {"iid": _institution_id(claims)},
    ))


@router.post("", response_model=TemplateOut, status_code=201)
async def create_template(
    body: TemplateCreate,
    claims: dict = Depends(current_claims),
):
    _require_admin(claims)
    institution_id = _institution_id(claims)
    with service_session() as conn:
        row = _one(conn.execute(
            text("""
                INSERT INTO institution.doc_template
                    (institution_id, product_id, product_code, code, name, description, doc_category, created_by)
                VALUES
                    (:iid, :product_id, :product_code, :code, :name, :description, :doc_category, :created_by)
                RETURNING *
            """),
            {
                "iid": institution_id,
                "product_id": str(body.product_id) if body.product_id else None,
                "product_code": body.product_code,
                "code": body.code,
                "name": body.name,
                "description": body.description,
                "doc_category": body.doc_category,
                "created_by": claims.get("sub"),
            },
        ))
        _audit(conn, claims=claims, action="doc_template.created",
               resource_type="doc_template", resource_id=row["id"], metadata=body.model_dump(mode="json"))
        conn.commit()
    return row


@router.patch("/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: UUID,
    body: TemplateUpdate,
    claims: dict = Depends(current_claims),
):
    _require_admin(claims)
    institution_id = _institution_id(claims)
    fields = body.model_dump(exclude_unset=True)
    with service_session() as conn:
        existing = _one(conn.execute(
            text("SELECT * FROM institution.doc_template WHERE id = :id AND institution_id = :iid"),
            {"id": str(template_id), "iid": institution_id},
        ))
        if not existing:
            raise HTTPException(status_code=404, detail="Template not found.")
        if not fields:
            return existing

        set_clause = ", ".join(f"{k} = :{k}" for k in fields) + ", updated_at = now()"
        row = _one(conn.execute(
            text(f"UPDATE institution.doc_template SET {set_clause} WHERE id = :id RETURNING *"),
            {**fields, "id": str(template_id)},
        ))
        _audit(conn, claims=claims, action="doc_template.updated",
               resource_type="doc_template", resource_id=template_id, metadata=fields)
        conn.commit()
    return row


@router.post("/{template_id}/retire", response_model=TemplateOut)
async def retire_template(
    template_id: UUID,
    claims: dict = Depends(current_claims),
):
    _require_admin(claims)
    institution_id = _institution_id(claims)
    with service_session() as conn:
        row = _one(conn.execute(
            text("""
                UPDATE institution.doc_template SET status = 'retired', updated_at = now()
                WHERE id = :id AND institution_id = :iid
                RETURNING *
            """),
            {"id": str(template_id), "iid": institution_id},
        ))
        if not row:
            raise HTTPException(status_code=404, detail="Template not found.")
        _audit(conn, claims=claims, action="doc_template.retired",
               resource_type="doc_template", resource_id=template_id)
        conn.commit()
    return row


# ---------------------------------------------------------------------------
# Versions (maker-checker publish)
# ---------------------------------------------------------------------------

@router.get("/{template_id}/versions", response_model=list[VersionOut])
async def list_versions(
    template_id: UUID,
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
):
    return _rows(conn.execute(
        text("""
            SELECT * FROM institution.doc_template_version
            WHERE template_id = :tid AND institution_id = :iid
            ORDER BY version_no DESC
        """),
        {"tid": str(template_id), "iid": _institution_id(claims)},
    ))


@router.post("/{template_id}/versions", response_model=VersionOut, status_code=201)
async def upload_version(
    template_id: UUID,
    file: UploadFile = File(...),
    change_note: str | None = Form(None),
    claims: dict = Depends(current_claims),
):
    """Maker step: upload a new draft version of a template's .docx."""
    _require_admin(claims)
    institution_id = _institution_id(claims)

    if file.content_type != ALLOWED_DOCX_MIME:
        raise HTTPException(status_code=400, detail="File must be a .docx (Word) document.")

    with service_session() as conn:
        template = _one(conn.execute(
            text("SELECT * FROM institution.doc_template WHERE id = :id AND institution_id = :iid"),
            {"id": str(template_id), "iid": institution_id},
        ))
        if not template:
            raise HTTPException(status_code=404, detail="Template not found.")

        next_version = template["current_version"] + 1

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / file.filename
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(file.file, f)

            checksum = merge_engine.sha256_of(tmp_path)
            size_bytes = tmp_path.stat().st_size
            storage_path = f"{_PREFIX}/{institution_id}/{template_id}/v{next_version}/{file.filename}"
            await _storage_upload(storage_path, tmp_path.read_bytes(), ALLOWED_DOCX_MIME)

        row = _one(conn.execute(
            text("""
                INSERT INTO institution.doc_template_version
                    (institution_id, template_id, version_no, file_name, storage_path,
                     file_size_bytes, checksum_sha256, change_note, created_by)
                VALUES
                    (:iid, :tid, :vno, :fname, :spath, :size, :checksum, :note, :creator)
                RETURNING *
            """),
            {
                "iid": institution_id, "tid": str(template_id), "vno": next_version,
                "fname": file.filename, "spath": storage_path, "size": size_bytes,
                "checksum": checksum, "note": change_note, "creator": claims.get("sub"),
            },
        ))
        _audit(conn, claims=claims, action="doc_template_version.uploaded",
               resource_type="doc_template_version", resource_id=row["id"],
               metadata={"version_no": next_version})
        conn.commit()
    return row


@router.post("/{template_id}/versions/{version_id}/decide", response_model=VersionOut)
async def decide_version(
    template_id: UUID,
    version_id: UUID,
    body: VersionDecision,
    claims: dict = Depends(current_claims),
):
    """Checker step: approve or reject a pending version. Maker != checker enforced."""
    _require_admin(claims)
    institution_id = _institution_id(claims)

    with service_session() as conn:
        version = _one(conn.execute(
            text("""
                SELECT * FROM institution.doc_template_version
                WHERE id = :vid AND template_id = :tid AND institution_id = :iid
            """),
            {"vid": str(version_id), "tid": str(template_id), "iid": institution_id},
        ))
        if not version:
            raise HTTPException(status_code=404, detail="Version not found.")
        if version["status"] not in ("draft", "pending_approval"):
            raise HTTPException(status_code=409, detail=f"Version is already {version['status']}.")
        if str(version["created_by"]) == str(claims.get("sub")):
            raise HTTPException(status_code=403, detail="Maker and checker must be different members.")

        if body.action == "approve":
            row = _one(conn.execute(
                text("""
                    UPDATE institution.doc_template_version
                    SET status = 'published', approved_by = :approver, approved_at = now()
                    WHERE id = :vid RETURNING *
                """),
                {"approver": claims.get("sub"), "vid": str(version_id)},
            ))
            conn.execute(
                text("""
                    UPDATE institution.doc_template
                    SET status = 'active', current_version = :vno, updated_at = now()
                    WHERE id = :tid
                """),
                {"vno": version["version_no"], "tid": str(template_id)},
            )
            event_type = "doc_template_version.approved"
        else:
            row = _one(conn.execute(
                text("""
                    UPDATE institution.doc_template_version
                    SET status = 'rejected', approved_by = :approver, approved_at = now(), rejection_note = :note
                    WHERE id = :vid RETURNING *
                """),
                {"approver": claims.get("sub"), "vid": str(version_id), "note": body.note},
            ))
            event_type = "doc_template_version.rejected"

        _audit(conn, claims=claims, action=event_type,
               resource_type="doc_template_version", resource_id=version_id, metadata={"note": body.note})
        conn.commit()
    return row


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _resolve_entity_snapshot(conn: Session, entity_type: str, entity_id: UUID, institution_id: str) -> dict:
    """Pull the merge-field data snapshot for the given deal/entity.

    loan_pipeline is the primary case: borrower, facility terms, schedule.
    Extend this map as new entity_type values are supported.
    """
    if entity_type == "loan_pipeline":
        row = _one(conn.execute(
            text("""
                SELECT lp.*, r.borrower_display_name, r.requested_amount, r.currency
                FROM marketplace.loan_pipeline lp
                JOIN marketplace.request r ON r.id = lp.request_id
                WHERE lp.id = :id AND lp.institution_id = :iid
            """),
            {"id": str(entity_id), "iid": institution_id},
        ))
        if not row:
            raise HTTPException(status_code=404, detail="loan_pipeline entity not found.")
        return row
    raise HTTPException(status_code=400, detail=f"Unsupported entity_type: {entity_type}")


@router.post("/{template_id}/generate", response_model=GenerationOut, status_code=201)
async def generate_document(
    template_id: UUID,
    body: GenerateRequest,
    claims: dict = Depends(current_claims),
):
    """Generate a populated document from the currently active (published) template version."""
    institution_id = _institution_id(claims)

    with service_session() as conn:
        template = _one(conn.execute(
            text("SELECT * FROM institution.doc_template WHERE id = :id AND institution_id = :iid"),
            {"id": str(template_id), "iid": institution_id},
        ))
        if not template:
            raise HTTPException(status_code=404, detail="Template not found.")
        if template["status"] != "active" or not template["current_version"]:
            raise HTTPException(status_code=409, detail="Template has no published/active version.")

        version = _one(conn.execute(
            text("""
                SELECT * FROM institution.doc_template_version
                WHERE template_id = :tid AND version_no = :vno AND status = 'published'
            """),
            {"tid": str(template_id), "vno": template["current_version"]},
        ))
        if not version:
            raise HTTPException(status_code=409, detail="Active version record missing or not published.")

        entity_snapshot = _resolve_entity_snapshot(conn, body.entity_type, body.entity_id, institution_id)
        context = merge_engine.resolve_context(entity_snapshot, body.data_overrides)

        gen_row = _one(conn.execute(
            text("""
                INSERT INTO institution.doc_generation
                    (institution_id, template_id, template_version_id, entity_type, entity_id,
                     stage_instance_id, data_snapshot, status, generated_by)
                VALUES
                    (:iid, :tid, :vid, :etype, :eid, :sid, CAST(:snap AS jsonb), 'generating', :generator)
                RETURNING *
            """),
            {
                "iid": institution_id, "tid": str(template_id), "vid": version["id"],
                "etype": body.entity_type, "eid": str(body.entity_id),
                "sid": str(body.stage_instance_id) if body.stage_instance_id else None,
                "snap": json.dumps(context, default=str),
                "generator": claims.get("sub"),
            },
        ))
        conn.commit()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                local_template = tmp_path / version["file_name"]
                data = await _storage_download(version["storage_path"])
                local_template.write_bytes(data)

                base_name = f"{template['code']}_{body.entity_id}"
                docx_path, pdf_path = merge_engine.generate(
                    local_template, context, tmp_path, base_name, make_pdf=body.output_pdf,
                )

                docx_storage_path = f"{_PREFIX}/{institution_id}/generated/{gen_row['id']}/{docx_path.name}"
                await _storage_upload(docx_storage_path, docx_path.read_bytes(), ALLOWED_DOCX_MIME)
                pdf_storage_path = None
                if pdf_path:
                    pdf_storage_path = f"{_PREFIX}/{institution_id}/generated/{gen_row['id']}/{pdf_path.name}"
                    await _storage_upload(pdf_storage_path, pdf_path.read_bytes(), "application/pdf")

            row = _one(conn.execute(
                text("""
                    UPDATE institution.doc_generation
                    SET status = 'generated', output_docx_path = :docx, output_pdf_path = :pdf,
                        generated_at = now()
                    WHERE id = :id RETURNING *
                """),
                {"docx": docx_storage_path, "pdf": pdf_storage_path, "id": gen_row["id"]},
            ))
            _audit(conn, claims=claims, action="doc_generation.generated",
                   resource_type="doc_generation", resource_id=gen_row["id"],
                   metadata={"template_version_id": str(version["id"])})
            conn.commit()
        except merge_engine.MergeError as exc:
            row = _one(conn.execute(
                text("""
                    UPDATE institution.doc_generation SET status = 'failed', error = :err
                    WHERE id = :id RETURNING *
                """),
                {"err": str(exc), "id": gen_row["id"]},
            ))
            _audit(conn, claims=claims, action="doc_generation.failed",
                   resource_type="doc_generation", resource_id=gen_row["id"], outcome="failure",
                   metadata={"error": str(exc)})
            conn.commit()
            return row

    return row


@router.get("/generations/{generation_id}/download")
async def download_generation(
    generation_id: UUID,
    fmt: str = "pdf",
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
):
    row = _one(conn.execute(
        text("SELECT * FROM institution.doc_generation WHERE id = :id AND institution_id = :iid"),
        {"id": str(generation_id), "iid": _institution_id(claims)},
    ))
    if not row or row["status"] != "generated":
        raise HTTPException(status_code=404, detail="Generated document not found.")

    path = row["output_pdf_path"] if fmt == "pdf" else row["output_docx_path"]
    if not path:
        raise HTTPException(status_code=404, detail=f"No {fmt} output for this generation.")

    data = await _storage_download(path)
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    return FileResponse(tmp_path, filename=Path(path).name)
