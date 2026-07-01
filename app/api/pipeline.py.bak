# =============================================================================
# ficium-portal-api — Pipeline module API
# Prefix: /pipelines  (institution-facing, JWT-authenticated)
#
# Endpoints:
#   GET  /pipelines               — list institution's pipelines
#   GET  /pipelines/{id}          — pipeline detail + stages + Phase 2 reveal
#   POST /pipelines/{id}/stages/{stage_id}/advance   — mark stage complete / submit for approval
#   POST /pipelines/{id}/stages/{stage_id}/approve   — checker approves (maker-checker stages)
#
# Architecture notes:
#   - Fully modular: no cross-module imports outside shared db/deps/config.
#   - All writes use service_session (bypass tenant RLS) so the portal user
#     can advance stages belonging to their institution.
#   - Institution scoping enforced via explicit institution_id guard on every query.
#   - maker-checker: advance -> status='awaiting_approval'; approve -> status='completed' + next stage activates.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import service_session
from ..deps import current_claims, tenant_conn
from .notifications import _write_notification

router = APIRouter(prefix="/pipelines", tags=["pipeline"])

def _rows(result) -> list[dict]:  return [dict(r._mapping) for r in result.fetchall()]
def _one(result)  -> dict | None:
    row = result.fetchone()
    return dict(row._mapping) if row else None


# ── GET /pipelines ─────────────────────────────────────────────────────────────
@router.get("")
async def list_pipelines(
    status: str = Query(default="active"),
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> list[dict]:
    """
    List all loan pipelines for the caller's institution.
    Returns summary rows with current stage info and SLA status.
    """
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    rows = _rows(conn.execute(text("""
        SELECT
            lp.id,
            lp.request_id,
            lp.status,
            -- anonymised consumer ref (first 8 chars of anon UUID)
            UPPER(LEFT(lp.consumer_id::text, 8))           AS consumer_ref,
            cp.label                                        AS product_label,
            lp.deal_amount,
            lp.deal_rate,
            lp.deal_term_months,
            lp.started_at,
            -- current stage denormalised
            psd.label                                       AS current_stage_label,
            psd.stage_key                                   AS current_stage_key,
            psi_cur.status                                  AS current_stage_status,
            psi_cur.id                                      AS current_stage_instance_id,
            psi_cur.sla_due_at                              AS current_sla_due_at,
            -- progress counts
            (SELECT COUNT(*) FROM marketplace.pipeline_stage_instance x
             WHERE x.pipeline_id = lp.id AND x.status = 'completed')::int AS stages_completed,
            (SELECT COUNT(*) FROM marketplace.pipeline_stage_instance x
             WHERE x.pipeline_id = lp.id)::int                            AS stages_total,
            -- SLA breached if active and past due
            (psi_cur.sla_due_at IS NOT NULL
             AND psi_cur.sla_due_at < now()
             AND psi_cur.status NOT IN ('completed','skipped'))            AS sla_breached
        FROM marketplace.loan_pipeline lp
        JOIN marketplace.pipeline_stage_instance psi_cur
            ON  psi_cur.id = lp.current_stage_id
        JOIN institution.pipeline_stage_def psd
            ON  psd.id = psi_cur.stage_def_id
        LEFT JOIN catalog.product cp
            ON  cp.id = (
                SELECT r.product_id FROM marketplace.request r WHERE r.id = lp.request_id
            )
        WHERE lp.institution_id = :iid
          AND (:status = 'all' OR lp.status = :status)
        ORDER BY lp.started_at DESC
    """), {"iid": institution_id, "status": status}))

    return rows


# ── GET /pipelines/{id} ────────────────────────────────────────────────────────
@router.get("/{pipeline_id}")
async def get_pipeline(
    pipeline_id: str,
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> dict:
    """
    Full pipeline detail: deal info, Phase 2 borrower identity, all stage instances.
    """
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    # Pipeline header + Phase 2 reveal
    pipeline = _one(conn.execute(text("""
        SELECT
            lp.id,
            lp.request_id,
            lp.bid_id,
            lp.status,
            lp.deal_amount,
            lp.deal_rate,
            lp.deal_term_months,
            lp.started_at,
            lp.completed_at,
            UPPER(LEFT(lp.consumer_id::text, 8))  AS consumer_ref,
            cp.label                               AS product_label,
            -- Phase 2 reveal from bid_acceptance
            ba.full_name    AS borrower_name,
            ba.email        AS borrower_email,
            ba.phone        AS borrower_phone,
            ba.address      AS borrower_address
        FROM marketplace.loan_pipeline lp
        LEFT JOIN catalog.product cp ON cp.id = (
            SELECT r.product_id FROM marketplace.request r WHERE r.id = lp.request_id
        )
        LEFT JOIN marketplace.bid_acceptance ba
            ON  ba.request_id = lp.request_id
            AND ba.institution_id = lp.institution_id
        WHERE lp.id = :pid AND lp.institution_id = :iid
    """), {"pid": pipeline_id, "iid": institution_id}))

    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found.")

    # Stage instances
    stages = _rows(conn.execute(text("""
        SELECT
            psi.id,
            psi.pipeline_id,
            psi.position,
            psi.status,
            psi.notes,
            psi.submitted_by,
            psi.submitted_at,
            psi.approved_by,
            psi.approved_at,
            psi.started_at,
            psi.completed_at,
            psi.sla_due_at,
            psi.updated_at,
            -- from stage def
            psd.stage_key,
            psd.label,
            psd.description,
            psd.borrower_label,
            psd.borrower_visible,
            psd.requires_maker_checker,
            psd.requires_documents,
            psd.sla_hours,
            -- SLA breach flag
            (psi.sla_due_at IS NOT NULL
             AND psi.sla_due_at < now()
             AND psi.status NOT IN ('completed','skipped'))  AS sla_breached,
            -- documents placeholder (extend when doc uploads are wired in)
            '[]'::jsonb AS documents
        FROM marketplace.pipeline_stage_instance psi
        JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
        WHERE psi.pipeline_id = :pid
        ORDER BY psi.position
    """), {"pid": pipeline_id}))

    return {**pipeline, "stages": stages}


# ── POST /pipelines/{id}/stages/{stage_id}/advance ────────────────────────────
@router.post("/{pipeline_id}/stages/{stage_id}/advance")
async def advance_stage(
    pipeline_id: str,
    stage_id:    str,
    body:        dict = Body(default={}),
    claims:      dict = Depends(current_claims),
) -> dict:
    """
    Advance an active stage.
    - If requires_maker_checker: status -> awaiting_approval (submitted by this user)
    - Else: status -> completed, next stage activated, current_stage_id updated.
    """
    institution_id = claims.get("institution_id")
    member_id      = claims.get("sub")
    notes          = body.get("notes")

    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as conn:
        # Guard: pipeline belongs to this institution
        pipeline = _one(conn.execute(text("""
            SELECT lp.id, lp.status, lp.current_stage_id
            FROM marketplace.loan_pipeline lp
            WHERE lp.id = :pid AND lp.institution_id = :iid
        """), {"pid": pipeline_id, "iid": institution_id}))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found.")
        if pipeline["status"] != "active":
            raise HTTPException(status_code=409, detail="Pipeline is not active.")

        # Guard: stage belongs to this pipeline and is active
        stage = _one(conn.execute(text("""
            SELECT psi.id, psi.status, psd.requires_maker_checker, psi.position
            FROM marketplace.pipeline_stage_instance psi
            JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
            WHERE psi.id = :sid AND psi.pipeline_id = :pid
        """), {"sid": stage_id, "pid": pipeline_id}))
        if not stage:
            raise HTTPException(status_code=404, detail="Stage not found.")
        if stage["status"] != "active":
            raise HTTPException(status_code=409,
                detail=f"Stage is '{stage['status']}', not active.")

        if stage["requires_maker_checker"]:
            # Maker submits — moves to awaiting_approval
            conn.execute(text("""
                UPDATE marketplace.pipeline_stage_instance
                SET status       = 'awaiting_approval',
                    submitted_by = :member,
                    submitted_at = now(),
                    notes        = COALESCE(:notes, notes),
                    updated_at   = now()
                WHERE id = :sid
            """), {"sid": stage_id, "member": member_id, "notes": notes})
            # Notify institution: a stage is waiting for checker approval
            _write_notification(
                conn, institution_id,
                kind="approval_needed",
                title="Stage awaiting approval",
                body="A pipeline stage requires checker sign-off.",
                link=f"/pipeline/{pipeline_id}",
                metadata={"pipeline_id": pipeline_id, "stage_id": stage_id},
            )
            conn.commit()
            return {"status": "awaiting_approval", "stage_id": stage_id, "pipeline_id": pipeline_id}
        else:
            if not member_id:
                raise HTTPException(status_code=403, detail="Member identity could not be resolved.")
            return _complete_stage(conn, pipeline_id, stage_id, stage["position"], member_id, notes, institution_id or "")


# ── POST /pipelines/{id}/stages/{stage_id}/approve ────────────────────────────
@router.post("/{pipeline_id}/stages/{stage_id}/approve")
async def approve_stage(
    pipeline_id: str,
    stage_id:    str,
    body:        dict = Body(default={}),
    claims:      dict = Depends(current_claims),
) -> dict:
    """
    Checker approves a stage in awaiting_approval state.
    Completes it and activates the next stage (or closes the pipeline if last).
    """
    institution_id = claims.get("institution_id")
    member_id      = claims.get("sub")
    notes          = body.get("notes")

    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as conn:
        pipeline = _one(conn.execute(text("""
            SELECT lp.id, lp.status FROM marketplace.loan_pipeline lp
            WHERE lp.id = :pid AND lp.institution_id = :iid
        """), {"pid": pipeline_id, "iid": institution_id}))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found.")

        stage = _one(conn.execute(text("""
            SELECT psi.id, psi.status, psi.submitted_by, psi.position
            FROM marketplace.pipeline_stage_instance psi
            WHERE psi.id = :sid AND psi.pipeline_id = :pid
        """), {"sid": stage_id, "pid": pipeline_id}))
        if not stage:
            raise HTTPException(status_code=404, detail="Stage not found.")
        if stage["status"] != "awaiting_approval":
            raise HTTPException(status_code=409,
                detail=f"Stage is '{stage['status']}', not awaiting approval.")
        # Basic 4-eyes: approver must differ from submitter
        if stage["submitted_by"] and stage["submitted_by"] == member_id:
            raise HTTPException(status_code=403,
                detail="Maker and checker must be different users.")

        conn.execute(text("""
            UPDATE marketplace.pipeline_stage_instance
            SET approved_by = :member, approved_at = now(),
                notes       = COALESCE(:notes, notes), updated_at = now()
            WHERE id = :sid
        """), {"sid": stage_id, "member": member_id, "notes": notes})

        if not member_id:
            raise HTTPException(status_code=403, detail="Member identity could not be resolved.")
        return _complete_stage(conn, pipeline_id, stage_id, stage["position"], member_id, notes, institution_id or "")


# ── Shared: complete a stage + activate next (or close pipeline) ───────────────
def _complete_stage(
    conn: Session,
    pipeline_id:    str,
    stage_id:       str,
    position:       int,
    member_id:      str,
    notes:          str | None,
    institution_id: str = "",
) -> dict:
    # Mark this stage complete
    conn.execute(text("""
        UPDATE marketplace.pipeline_stage_instance
        SET status       = 'completed',
            completed_at = now(),
            notes        = COALESCE(:notes, notes),
            updated_at   = now()
        WHERE id = :sid
    """), {"sid": stage_id, "notes": notes})

    # Find next pending stage
    next_stage = _one(conn.execute(text("""
        SELECT psi.id FROM marketplace.pipeline_stage_instance psi
        WHERE psi.pipeline_id = :pid AND psi.position > :pos
          AND psi.status = 'pending'
        ORDER BY psi.position LIMIT 1
    """), {"pid": pipeline_id, "pos": position}))

    if next_stage:
        # Activate next stage
        conn.execute(text("""
            UPDATE marketplace.pipeline_stage_instance
            SET status = 'active', started_at = now(), updated_at = now()
            WHERE id = :nid
        """), {"nid": next_stage["id"]})
        conn.execute(text("""
            UPDATE marketplace.loan_pipeline
            SET current_stage_id = :nid, updated_at = now()
            WHERE id = :pid
        """), {"nid": next_stage["id"], "pid": pipeline_id})
        # Notify institution: pipeline advanced to next stage
        _write_notification(
            conn, institution_id,
            kind="pipeline_advanced",
            title="Pipeline stage completed",
            body="A processing stage has been completed and the next stage is now active.",
            link=f"/pipeline/{pipeline_id}",
            metadata={"pipeline_id": pipeline_id, "completed_stage_id": stage_id},
        )
        conn.commit()
        return {"status": "advanced", "next_stage_id": next_stage["id"], "pipeline_id": pipeline_id}
    else:
        # All stages done — close pipeline
        conn.execute(text("""
            UPDATE marketplace.loan_pipeline
            SET status = 'completed', completed_at = now(), updated_at = now()
            WHERE id = :pid
        """), {"pid": pipeline_id})
        conn.commit()
        return {"status": "completed", "pipeline_id": pipeline_id}
