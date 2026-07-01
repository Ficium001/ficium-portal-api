# =============================================================================
# ficium-portal-api — Pipeline Template Config API
# Prefix: /pipelines/templates  (institution-facing, JWT-authenticated)
#
# Endpoints:
#   GET    /pipelines/templates                     — list institution templates
#   POST   /pipelines/templates                     — create template (+stages)
#   GET    /pipelines/templates/{id}                — template detail + stages
#   PUT    /pipelines/templates/{id}                — update name/description/active
#   POST   /pipelines/templates/{id}/stages         — add stage to template
#   PUT    /pipelines/templates/{id}/stages/{sid}   — update a stage
#   DELETE /pipelines/templates/{id}/stages/{sid}   — delete a stage
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import service_session
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/pipelines/templates", tags=["pipeline-templates"])

def _rows(result) -> list[dict]: return [dict(r._mapping) for r in result.fetchall()]
def _one(result)  -> dict | None:
    row = result.fetchone()
    return dict(row._mapping) if row else None


VALID_PRODUCT_TYPES = {
    "personal_loan", "sme_loan", "mortgage",
    "auto_loan", "education_loan", "general",
}
VALID_STAGE_TYPES = {
    "credit_docs", "offer_letter", "legal_review",
    "approval_gate", "custom",
}


# ── GET /pipelines/templates ───────────────────────────────────────────────────
@router.get("")
async def list_templates(
    claims: dict    = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> list[dict]:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    return _rows(conn.execute(text("""
        SELECT
            t.id,
            t.name,
            t.description,
            t.product_type,
            t.is_active,
            t.created_at,
            t.updated_at,
            COUNT(s.id)::int AS stage_count
        FROM workflow.template t
        LEFT JOIN workflow.stage s ON s.template_id = t.id
        WHERE t.institution_id = :iid
        GROUP BY t.id
        ORDER BY t.product_type, t.created_at
    """), {"iid": institution_id}))


# ── POST /pipelines/templates ──────────────────────────────────────────────────
@router.post("", status_code=201)
async def create_template(
    body:   dict = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    institution_id = claims.get("institution_id")
    member_id      = claims.get("sub")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    name         = (body.get("name") or "").strip()
    product_type = (body.get("product_type") or "").strip()
    description  = (body.get("description") or "").strip() or None
    stages_input = body.get("stages") or []

    if not name:
        raise HTTPException(status_code=422, detail="name is required.")
    if product_type not in VALID_PRODUCT_TYPES:
        raise HTTPException(status_code=422,
            detail=f"product_type must be one of: {', '.join(sorted(VALID_PRODUCT_TYPES))}")

    with service_session() as sconn:
        existing = _one(sconn.execute(text("""
            SELECT id FROM workflow.template
            WHERE institution_id = :iid AND product_type = :pt AND is_active = true
        """), {"iid": institution_id, "pt": product_type}))
        if existing:
            raise HTTPException(status_code=409,
                detail=f"An active template for '{product_type}' already exists.")

        tmpl = _one(sconn.execute(text("""
            INSERT INTO workflow.template (institution_id, product_type, name, description, created_by)
            VALUES (:iid, :pt, :name, :desc, :creator)
            RETURNING id, name, product_type, description, is_active, created_at, updated_at
        """), {
            "iid": institution_id, "pt": product_type,
            "name": name, "desc": description, "creator": member_id,
        }))

        stages_created = []
        for pos, s in enumerate(stages_input, start=1):
            st = (s.get("stage_type") or "custom").strip()
            if st not in VALID_STAGE_TYPES:
                st = "custom"
            row = _one(sconn.execute(text("""
                INSERT INTO workflow.stage
                    (template_id, position, name, stage_type, description, is_required, sla_hours)
                VALUES (:tid, :pos, :name, :st, :desc, :req, :sla)
                RETURNING id, position, name, stage_type, description, is_required, sla_hours
            """), {
                "tid": tmpl["id"], "pos": pos,
                "name": (s.get("name") or st).strip(), "st": st,
                "desc": (s.get("description") or "").strip() or None,
                "req": s.get("is_required", True),
                "sla": s.get("sla_hours") or None,
            }))
            stages_created.append(row)

        sconn.commit()
        return {**tmpl, "stage_count": len(stages_created), "stages": stages_created}


# ── GET /pipelines/templates/{id} ─────────────────────────────────────────────
@router.get("/{template_id}")
async def get_template(
    template_id: str,
    claims:      dict    = Depends(current_claims),
    conn:        Session = Depends(tenant_conn),
) -> dict:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    tmpl = _one(conn.execute(text("""
        SELECT id, name, description, product_type, is_active, created_at, updated_at
        FROM workflow.template
        WHERE id = :tid AND institution_id = :iid
    """), {"tid": template_id, "iid": institution_id}))
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found.")

    stages = _rows(conn.execute(text("""
        SELECT id, position, name, stage_type, description, is_required, sla_hours, created_at
        FROM workflow.stage
        WHERE template_id = :tid
        ORDER BY position
    """), {"tid": template_id}))

    return {**tmpl, "stages": stages}


# ── PUT /pipelines/templates/{id} ─────────────────────────────────────────────
@router.put("/{template_id}")
async def update_template(
    template_id: str,
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
) -> dict:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as sconn:
        existing = _one(sconn.execute(text("""
            SELECT id FROM workflow.template
            WHERE id = :tid AND institution_id = :iid
        """), {"tid": template_id, "iid": institution_id}))
        if not existing:
            raise HTTPException(status_code=404, detail="Template not found.")

        tmpl = _one(sconn.execute(text("""
            UPDATE workflow.template
            SET name        = COALESCE(:name,   name),
                description = COALESCE(:desc,   description),
                is_active   = COALESCE(:active, is_active),
                updated_at  = now()
            WHERE id = :tid
            RETURNING id, name, description, product_type, is_active, created_at, updated_at
        """), {
            "tid":    template_id,
            "name":   (body.get("name") or "").strip() or None,
            "desc":   body.get("description"),
            "active": body.get("is_active"),
        }))
        sconn.commit()
        return tmpl


# ── POST /pipelines/templates/{id}/stages ─────────────────────────────────────
@router.post("/{template_id}/stages", status_code=201)
async def add_stage(
    template_id: str,
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
) -> dict:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    stage_type = (body.get("stage_type") or "custom").strip()
    if stage_type not in VALID_STAGE_TYPES:
        stage_type = "custom"

    with service_session() as sconn:
        tmpl = _one(sconn.execute(text("""
            SELECT id FROM workflow.template
            WHERE id = :tid AND institution_id = :iid
        """), {"tid": template_id, "iid": institution_id}))
        if not tmpl:
            raise HTTPException(status_code=404, detail="Template not found.")

        max_pos = _one(sconn.execute(text("""
            SELECT COALESCE(MAX(position), 0) AS max_pos
            FROM workflow.stage WHERE template_id = :tid
        """), {"tid": template_id}))["max_pos"]

        stage = _one(sconn.execute(text("""
            INSERT INTO workflow.stage
                (template_id, position, name, stage_type, description, is_required, sla_hours)
            VALUES (:tid, :pos, :name, :st, :desc, :req, :sla)
            RETURNING id, position, name, stage_type, description, is_required, sla_hours, created_at
        """), {
            "tid": template_id, "pos": max_pos + 1,
            "name": (body.get("name") or stage_type).strip(), "st": stage_type,
            "desc": (body.get("description") or "").strip() or None,
            "req": body.get("is_required", True),
            "sla": body.get("sla_hours") or None,
        }))
        sconn.commit()
        return stage


# ── PUT /pipelines/templates/{id}/stages/{sid} ────────────────────────────────
@router.put("/{template_id}/stages/{stage_id}")
async def update_stage(
    template_id: str,
    stage_id:    str,
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
) -> dict:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as sconn:
        existing = _one(sconn.execute(text("""
            SELECT s.id FROM workflow.stage s
            JOIN workflow.template t ON t.id = s.template_id
            WHERE s.id = :sid AND s.template_id = :tid AND t.institution_id = :iid
        """), {"sid": stage_id, "tid": template_id, "iid": institution_id}))
        if not existing:
            raise HTTPException(status_code=404, detail="Stage not found.")

        stage = _one(sconn.execute(text("""
            UPDATE workflow.stage
            SET name        = COALESCE(:name, name),
                stage_type  = COALESCE(:st,   stage_type),
                description = COALESCE(:desc,  description),
                is_required = COALESCE(:req,   is_required),
                sla_hours   = COALESCE(:sla,   sla_hours)
            WHERE id = :sid
            RETURNING id, position, name, stage_type, description, is_required, sla_hours
        """), {
            "sid":  stage_id,
            "name": (body.get("name") or "").strip() or None,
            "st":   body.get("stage_type"),
            "desc": body.get("description"),
            "req":  body.get("is_required"),
            "sla":  body.get("sla_hours"),
        }))
        sconn.commit()
        return stage


# ── DELETE /pipelines/templates/{id}/stages/{sid} ─────────────────────────────
@router.delete("/{template_id}/stages/{stage_id}")
async def delete_stage(
    template_id: str,
    stage_id:    str,
    claims:      dict = Depends(current_claims),
) -> Response:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as sconn:
        row = _one(sconn.execute(text("""
            SELECT s.id FROM workflow.stage s
            JOIN workflow.template t ON t.id = s.template_id
            WHERE s.id = :sid AND s.template_id = :tid AND t.institution_id = :iid
        """), {"sid": stage_id, "tid": template_id, "iid": institution_id}))
        if not row:
            raise HTTPException(status_code=404, detail="Stage not found.")

        sconn.execute(text("DELETE FROM workflow.stage WHERE id = :sid"), {"sid": stage_id})
        # Re-number remaining stages to stay contiguous
        sconn.execute(text("""
            WITH ranked AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY position) AS new_pos
                FROM workflow.stage WHERE template_id = :tid
            )
            UPDATE workflow.stage s
            SET position = ranked.new_pos
            FROM ranked WHERE s.id = ranked.id
        """), {"tid": template_id})
        sconn.commit()
    return Response(status_code=204)
