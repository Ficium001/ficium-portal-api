# =============================================================================
# ficium-portal-api — Documents router
#
# Module gate:  inst:documents  (institution members)
#               admin role      (approve / reject)
#
# Storage:      Supabase Storage — files uploaded client-side via signed URL.
#               This router manages the metadata row only.
#
# Endpoints:
#   GET  /documents/types              — Ficium-seeded doc type reference
#   GET  /documents                    — institution's documents + status
#   POST /documents                    — register uploaded doc (metadata only)
#   GET  /documents/compliance         — compliance gate status for caller's institution
#   POST /documents/{id}/review        — admin: approve or reject a document
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import service_session
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/documents", tags=["documents"])

_MODULE  = "inst:documents"
_ADMINS  = ("admin", "super_admin")


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


def _require_module(claims: dict) -> None:
    perms: list[str] = claims.get("module_permissions", [])
    if "*" not in perms and _MODULE not in perms:
        raise HTTPException(status_code=403, detail="Module access denied: inst:documents")


def _institution_id(claims: dict) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="No institution context.")
    return iid


# ─── GET /documents/types ──────────────────────────────────────────────────────

@router.get("/types")
async def list_doc_types(
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> list[dict]:
    """Ficium-seeded required document types."""
    _require_module(claims)
    result = conn.execute(
        text("SELECT * FROM institution.doc_type ORDER BY sort_order")
    )
    return _rows(result)


# ─── GET /documents/compliance ─────────────────────────────────────────────────

@router.get("/compliance")
async def get_compliance(
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> dict:
    """Compliance gate status — used to enable/disable bid submission."""
    _require_module(claims)
    institution_id = _institution_id(claims)

    row = conn.execute(
        text("""
            SELECT
                institution_id,
                is_approved,
                is_compliant,
                missing_docs,
                can_bid
            FROM institution.compliance
            WHERE institution_id = :iid
        """),
        {"iid": institution_id},
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Institution not found.")
    return dict(row._mapping)


# ─── GET /documents ────────────────────────────────────────────────────────────

@router.get("")
async def list_documents(
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> list[dict]:
    """All documents for the caller's institution, enriched with doc type info."""
    _require_module(claims)
    result = conn.execute(
        text("""
            SELECT
                d.*,
                dt.code        AS doc_type_code,
                dt.label       AS doc_type_label,
                dt.is_mandatory,
                dt.description AS doc_type_description
            FROM institution.doc d
            JOIN institution.doc_type dt ON dt.id = d.doc_type_id
            ORDER BY dt.sort_order, d.uploaded_at DESC
        """)
    )
    return _rows(result)


# ─── POST /documents ───────────────────────────────────────────────────────────

@router.post("")
async def register_document(
    body:   dict = Body(...),
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> dict:
    """
    Register an uploaded document (metadata only).
    File must be uploaded to Supabase Storage client-side first.
    body: { doc_type_id, storage_path, file_name, mime_type, expiry_date? }
    """
    _require_module(claims)
    institution_id = _institution_id(claims)
    member_id      = claims.get("member_id")

    required = {"doc_type_id", "storage_path", "file_name"}
    missing  = required - set(body)
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {sorted(missing)}")

    # Validate doc_type exists
    dt = conn.execute(
        text("SELECT id FROM institution.doc_type WHERE id = :id"),
        {"id": body["doc_type_id"]},
    ).fetchone()
    if dt is None:
        raise HTTPException(status_code=422, detail="Invalid doc_type_id.")

    # Upsert — one row per institution+doc_type, new upload resets to pending
    row = conn.execute(
        text("""
            INSERT INTO institution.doc (
                institution_id, doc_type_id,
                storage_path, file_name, mime_type,
                status, expiry_date,
                uploaded_by, uploaded_at
            ) VALUES (
                :iid, :doc_type_id,
                :storage_path, :file_name, :mime_type,
                'pending', :expiry_date,
                :uploaded_by, now()
            )
            ON CONFLICT (institution_id, doc_type_id)
            DO UPDATE SET
                storage_path  = EXCLUDED.storage_path,
                file_name     = EXCLUDED.file_name,
                mime_type     = EXCLUDED.mime_type,
                status        = 'pending',
                expiry_date   = EXCLUDED.expiry_date,
                uploaded_by   = EXCLUDED.uploaded_by,
                uploaded_at   = now(),
                rejection_reason = NULL,
                reviewed_by   = NULL,
                reviewed_at   = NULL
            RETURNING *
        """),
        {
            "iid":          institution_id,
            "doc_type_id":  body["doc_type_id"],
            "storage_path": body["storage_path"],
            "file_name":    body["file_name"],
            "mime_type":    body.get("mime_type"),
            "expiry_date":  body.get("expiry_date"),
            "uploaded_by":  member_id,
        },
    ).fetchone()
    conn.commit()
    if row is None:
        raise HTTPException(status_code=500, detail="Document record could not be created.")
    return dict(row._mapping)


# ─── POST /documents/{id}/review ───────────────────────────────────────────────

@router.post("/{doc_id}/review")
async def review_document(
    doc_id: str  = Path(...),
    body:   dict = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    """
    Admin-only: approve or reject a document.
    body: { action: 'approve' | 'reject', rejection_reason?: str }
    Uses service session (bypasses institution RLS — admin cross-institution access).
    """
    if claims.get("user_role") not in _ADMINS:
        raise HTTPException(status_code=403, detail="Admin access required.")

    action = body.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="action must be 'approve' or 'reject'.")

    if action == "reject" and not body.get("rejection_reason"):
        raise HTTPException(status_code=422, detail="rejection_reason required when rejecting.")

    new_status = "approved" if action == "approve" else "rejected"
    admin_id   = claims.get("sub")

    with service_session() as conn:
        row = conn.execute(
            text("""
                UPDATE institution.doc
                SET
                    status           = :status,
                    rejection_reason = :reason,
                    reviewed_by      = :reviewer,
                    reviewed_at      = now()
                WHERE id = :id
                RETURNING id, institution_id, status, rejection_reason
            """),
            {
                "id":       doc_id,
                "status":   new_status,
                "reason":   body.get("rejection_reason"),
                "reviewer": admin_id,
            },
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        conn.commit()

    return dict(row._mapping)


# ─── POST /documents/upload-url ────────────────────────────────────────────────

@router.post("/upload-url")
async def get_upload_url(
    body:   dict = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    """
    Generate a Supabase Storage signed upload URL.
    body: { doc_type_id, file_name, mime_type }
    Returns: { upload_url, storage_path }
    """
    _require_module(claims)
    institution_id = _institution_id(claims)

    import os
    import uuid as _uuid

    import httpx
    file_name   = body.get("file_name", "file")
    ext         = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
    object_name = f"{institution_id}/{body.get('doc_type_id', 'misc')}/{_uuid.uuid4()}.{ext}"

    supabase_url = os.environ["PORTAL_SUPABASE_URL"]
    service_key  = os.environ["PORTAL_SUPABASE_SERVICE_KEY"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{supabase_url}/storage/v1/object/upload/sign/institution-docs/{object_name}",
            headers={"Authorization": f"Bearer {service_key}", "Content-Type": "application/json"},
            json={"upsert": True},
        )
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail="Could not generate upload URL.")
        data = resp.json()

    signed_url = f"{supabase_url}/storage/v1{data['url']}"
    return {"upload_url": signed_url, "storage_path": object_name}
