# =============================================================================
# ficium-portal-api — Pipeline Template Config API
# Prefix: /pipelines/templates  (institution-facing, JWT-authenticated)
#
# Targets the EXECUTION schema: institution.pipeline_template +
# institution.pipeline_stage_def — the same tables read by
# marketplace.create_pipeline_from_acceptance().
#
# Endpoints:
#   GET    /pipelines/templates                     — list templates
#   POST   /pipelines/templates                     — create template (+stages)
#   GET    /pipelines/templates/{id}                — detail + stages
#   PUT    /pipelines/templates/{id}                — update name/description/default/active
#   POST   /pipelines/templates/{id}/stages         — add stage
#   PUT    /pipelines/templates/{id}/stages/{sid}   — update stage
#   DELETE /pipelines/templates/{id}/stages/{sid}   — delete + renumber
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


# Valid stage_key values (mirrors institution.stage_key_enum)
VALID_STAGE_KEYS = {
    "credit_docs", "offer_letter", "legal_review",
    "board_approval", "disbursement", "custom",
}


# ── GET /pipelines/templates ───────────────────────────────────────────────────
@router.get("")
async def list_templates(
    claims: dict    = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> list[dict]:
    """List all pipeline templates for the caller's institution."""
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    return _rows(conn.execute(text("""
        SELECT
            t.id,
            t.name,
            t.description,
            t.product_code,
            t.is_default,
            t.is_active,
            t.created_at,
            t.updated_at,
            COUNT(s.id)::int AS stage_count,
            cp.label         AS product_label
        FROM institution.pipeline_template t
        LEFT JOIN institution.pipeline_stage_def s
            ON s.template_id = t.id AND s.is_active = true
        LEFT JOIN catalog.product cp ON cp.code = t.product_code
        WHERE t.institution_id = :iid
        GROUP BY t.id, cp.label
        ORDER BY t.is_default DESC, t.product_code NULLS LAST, t.created_at
    """), {"iid": institution_id}))


# ── POST /pipelines/templates ──────────────────────────────────────────────────
@router.post("", status_code=201)
async def create_template(
    body:   dict = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    """
    Create a template. product_code=null means it applies to ALL products (default fallback).
    Only one active default per institution is allowed.
    Body: { name, product_code?, description?, is_default?, stages?: [...] }
    """
    institution_id = claims.get("institution_id")
    member_id      = claims.get("sub")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    name         = (body.get("name") or "").strip()
    product_code = body.get("product_code") or None   # None = applies to all products
    description  = (body.get("description") or "").strip() or None
    is_default   = bool(body.get("is_default", product_code is None))
    stages_input = body.get("stages") or []

    if not name:
        raise HTTPException(status_code=422, detail="name is required.")

    with service_session() as sconn:
        # Validate product_code exists in catalog (if provided)
        if product_code:
            exists = _one(sconn.execute(text(
                "SELECT code FROM catalog.product WHERE code = :code"
            ), {"code": product_code}))
            if not exists:
                raise HTTPException(status_code=422,
                    detail=f"product_code '{product_code}' not found in catalog.")

        # Enforce: only one active template per institution × product_code
        conflict_q = """
            SELECT id FROM institution.pipeline_template
            WHERE institution_id = :iid
              AND is_active = true
              AND (
                (:product_code IS NULL AND product_code IS NULL)
                OR product_code = :product_code
              )
        """
        existing = _one(sconn.execute(text(conflict_q),
            {"iid": institution_id, "product_code": product_code}))
        if existing:
            scope = f"'{product_code}'" if product_code else "default (all products)"
            raise HTTPException(status_code=409,
                detail=f"An active template for {scope} already exists. Deactivate it first.")

        # If setting as default, clear any existing default
        if is_default:
            sconn.execute(text("""
                UPDATE institution.pipeline_template
                SET is_default = false, updated_at = now()
                WHERE institution_id = :iid AND is_default = true
            """), {"iid": institution_id})

        tmpl = _one(sconn.execute(text("""
            INSERT INTO institution.pipeline_template
                (institution_id, name, description, product_code, is_default, created_by)
            VALUES (:iid, :name, :desc, :code, :dflt, :creator)
            RETURNING id, name, description, product_code, is_default, is_active,
                      created_at, updated_at
        """), {
            "iid": institution_id, "name": name, "desc": description,
            "code": product_code, "dflt": is_default, "creator": member_id,
        }))
        if tmpl is None:
            raise HTTPException(status_code=500, detail="Template insert returned no row.")

        stages_created = []
        for pos, s in enumerate(stages_input, start=1):
            stage_key = (s.get("stage_key") or "custom").strip()
            if stage_key not in VALID_STAGE_KEYS:
                stage_key = "custom"
            row = _one(sconn.execute(text("""
                INSERT INTO institution.pipeline_stage_def
                    (template_id, institution_id, stage_key, label, description,
                     position, sla_hours, requires_maker_checker, requires_documents,
                     borrower_label, borrower_visible)
                VALUES (:tid, :iid, :sk::institution.stage_key_enum, :label, :desc,
                        :pos, :sla, :rmc, :rdoc, :blabel, :bvis)
                RETURNING id, position, stage_key, label, description, sla_hours,
                          requires_maker_checker, requires_documents,
                          borrower_label, borrower_visible, is_active
            """), {
                "tid":    tmpl["id"],
                "iid":    institution_id,
                "sk":     stage_key,
                "label":  (s.get("label") or stage_key).strip(),
                "desc":   (s.get("description") or "").strip() or None,
                "pos":    pos,
                "sla":    s.get("sla_hours", 48),
                "rmc":    s.get("requires_maker_checker", False),
                "rdoc":   s.get("requires_documents", False),
                "blabel": (s.get("borrower_label") or "").strip() or None,
                "bvis":   s.get("borrower_visible", True),
            }))
            stages_created.append(row)

        sconn.commit()
        return {**tmpl, "stage_count": len(stages_created), "stages": stages_created,
                "product_label": None}


# ── GET /pipelines/templates/{id} ─────────────────────────────────────────────
@router.get("/{template_id}")
async def get_template(
    template_id: str,
    claims:      dict    = Depends(current_claims),
    conn:        Session = Depends(tenant_conn),
) -> dict:
    """Template detail with all active stages ordered by position."""
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    tmpl = _one(conn.execute(text("""
        SELECT t.id, t.name, t.description, t.product_code, t.is_default,
               t.is_active, t.created_at, t.updated_at,
               cp.label AS product_label
        FROM institution.pipeline_template t
        LEFT JOIN catalog.product cp ON cp.code = t.product_code
        WHERE t.id = :tid AND t.institution_id = :iid
    """), {"tid": template_id, "iid": institution_id}))
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found.")

    stages = _rows(conn.execute(text("""
        SELECT id, position, stage_key, label, description, sla_hours,
               requires_maker_checker, requires_documents,
               borrower_label, borrower_visible, is_active, created_at
        FROM institution.pipeline_stage_def
        WHERE template_id = :tid AND institution_id = :iid
        ORDER BY position
    """), {"tid": template_id, "iid": institution_id}))

    return {**tmpl, "stages": stages}


# ── PUT /pipelines/templates/{id} ─────────────────────────────────────────────
@router.put("/{template_id}")
async def update_template(
    template_id: str,
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
) -> dict:
    """Update name / description / is_default / is_active."""
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as sconn:
        existing = _one(sconn.execute(text("""
            SELECT id, is_default FROM institution.pipeline_template
            WHERE id = :tid AND institution_id = :iid
        """), {"tid": template_id, "iid": institution_id}))
        if not existing:
            raise HTTPException(status_code=404, detail="Template not found.")

        # If promoting to default, demote the current default first
        if body.get("is_default") is True and not existing["is_default"]:
            sconn.execute(text("""
                UPDATE institution.pipeline_template
                SET is_default = false, updated_at = now()
                WHERE institution_id = :iid AND is_default = true AND id != :tid
            """), {"iid": institution_id, "tid": template_id})

        tmpl = _one(sconn.execute(text("""
            UPDATE institution.pipeline_template
            SET name        = COALESCE(:name,   name),
                description = COALESCE(:desc,   description),
                is_default  = COALESCE(:dflt,   is_default),
                is_active   = COALESCE(:active, is_active),
                updated_at  = now()
            WHERE id = :tid
            RETURNING id, name, description, product_code, is_default,
                      is_active, created_at, updated_at
        """), {
            "tid":    template_id,
            "name":   (body.get("name") or "").strip() or None,
            "desc":   body.get("description"),
            "dflt":   body.get("is_default"),
            "active": body.get("is_active"),
        }))
        sconn.commit()
        if tmpl is None:
            raise HTTPException(status_code=404, detail="Template not found.")
        return tmpl


# ── POST /pipelines/templates/{id}/stages ─────────────────────────────────────
@router.post("/{template_id}/stages", status_code=201)
async def add_stage(
    template_id: str,
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
) -> dict:
    """Append a new stage to an existing template."""
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    stage_key = (body.get("stage_key") or "custom").strip()
    if stage_key not in VALID_STAGE_KEYS:
        stage_key = "custom"

    label = (body.get("label") or stage_key).strip()
    if not label:
        raise HTTPException(status_code=422, detail="label is required.")

    with service_session() as sconn:
        tmpl = _one(sconn.execute(text("""
            SELECT id FROM institution.pipeline_template
            WHERE id = :tid AND institution_id = :iid
        """), {"tid": template_id, "iid": institution_id}))
        if not tmpl:
            raise HTTPException(status_code=404, detail="Template not found.")

        max_pos_row = _one(sconn.execute(text("""
            SELECT COALESCE(MAX(position), 0) AS max_pos
            FROM institution.pipeline_stage_def
            WHERE template_id = :tid AND institution_id = :iid
        """), {"tid": template_id, "iid": institution_id}))
        if max_pos_row is None:
            raise HTTPException(status_code=500, detail="Position query returned no row.")
        max_pos = max_pos_row["max_pos"]

        stage = _one(sconn.execute(text("""
            INSERT INTO institution.pipeline_stage_def
                (template_id, institution_id, stage_key, label, description,
                 position, sla_hours, requires_maker_checker, requires_documents,
                 borrower_label, borrower_visible)
            VALUES (:tid, :iid, :sk::institution.stage_key_enum, :label, :desc,
                    :pos, :sla, :rmc, :rdoc, :blabel, :bvis)
            RETURNING id, position, stage_key, label, description, sla_hours,
                      requires_maker_checker, requires_documents,
                      borrower_label, borrower_visible, is_active, created_at
        """), {
            "tid":    template_id,
            "iid":    institution_id,
            "sk":     stage_key,
            "label":  label,
            "desc":   (body.get("description") or "").strip() or None,
            "pos":    max_pos + 1,
            "sla":    body.get("sla_hours", 48),
            "rmc":    body.get("requires_maker_checker", False),
            "rdoc":   body.get("requires_documents", False),
            "blabel": (body.get("borrower_label") or "").strip() or None,
            "bvis":   body.get("borrower_visible", True),
        }))
        sconn.commit()
        if stage is None:
            raise HTTPException(status_code=500, detail="Stage insert returned no row.")
        return stage


# ── PUT /pipelines/templates/{id}/stages/{sid} ────────────────────────────────
@router.put("/{template_id}/stages/{stage_id}")
async def update_stage(
    template_id: str,
    stage_id:    str,
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
) -> dict:
    """Update any stage field. stage_key, sla_hours, maker_checker, borrower config."""
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as sconn:
        existing = _one(sconn.execute(text("""
            SELECT id FROM institution.pipeline_stage_def
            WHERE id = :sid AND template_id = :tid AND institution_id = :iid
        """), {"sid": stage_id, "tid": template_id, "iid": institution_id}))
        if not existing:
            raise HTTPException(status_code=404, detail="Stage not found.")

        # Build stage_key cast only if provided
        sk_val = body.get("stage_key")
        if sk_val and sk_val not in VALID_STAGE_KEYS:
            sk_val = "custom"

        stage = _one(sconn.execute(text("""
            UPDATE institution.pipeline_stage_def
            SET stage_key              = CASE WHEN :sk IS NOT NULL
                                              THEN :sk::institution.stage_key_enum
                                              ELSE stage_key END,
                label                  = COALESCE(:label, label),
                description            = COALESCE(:desc,  description),
                sla_hours              = COALESCE(:sla,   sla_hours),
                requires_maker_checker = COALESCE(:rmc,   requires_maker_checker),
                requires_documents     = COALESCE(:rdoc,  requires_documents),
                borrower_label         = COALESCE(:blabel, borrower_label),
                borrower_visible       = COALESCE(:bvis,   borrower_visible)
            WHERE id = :sid
            RETURNING id, position, stage_key, label, description, sla_hours,
                      requires_maker_checker, requires_documents,
                      borrower_label, borrower_visible, is_active
        """), {
            "sid":    stage_id,
            "sk":     sk_val,
            "label":  (body.get("label") or "").strip() or None,
            "desc":   body.get("description"),
            "sla":    body.get("sla_hours"),
            "rmc":    body.get("requires_maker_checker"),
            "rdoc":   body.get("requires_documents"),
            "blabel": body.get("borrower_label"),
            "bvis":   body.get("borrower_visible"),
        }))
        sconn.commit()
        if stage is None:
            raise HTTPException(status_code=404, detail="Stage not found.")
        return stage


# ── DELETE /pipelines/templates/{id}/stages/{sid} ─────────────────────────────
@router.delete("/{template_id}/stages/{stage_id}")
async def delete_stage(
    template_id: str,
    stage_id:    str,
    claims:      dict = Depends(current_claims),
) -> Response:
    """Remove a stage and renumber remaining stages to stay contiguous."""
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as sconn:
        row = _one(sconn.execute(text("""
            SELECT id FROM institution.pipeline_stage_def
            WHERE id = :sid AND template_id = :tid AND institution_id = :iid
        """), {"sid": stage_id, "tid": template_id, "iid": institution_id}))
        if not row:
            raise HTTPException(status_code=404, detail="Stage not found.")

        sconn.execute(text(
            "DELETE FROM institution.pipeline_stage_def WHERE id = :sid"
        ), {"sid": stage_id})

        # Renumber remaining stages to stay contiguous
        sconn.execute(text("""
            WITH ranked AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY position) AS new_pos
                FROM institution.pipeline_stage_def
                WHERE template_id = :tid AND institution_id = :iid
            )
            UPDATE institution.pipeline_stage_def s
            SET position = ranked.new_pos
            FROM ranked WHERE s.id = ranked.id
        """), {"tid": template_id, "iid": institution_id})

        sconn.commit()
    return Response(status_code=204)
