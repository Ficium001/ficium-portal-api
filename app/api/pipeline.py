# =============================================================================
# ficium-portal-api — Pipeline module API
# Prefix: /pipelines  (institution-facing, JWT-authenticated)
#
# Endpoints:
#   GET  /pipelines               — list institution's pipelines
#   GET  /pipelines/{id}          — pipeline detail + stages + Phase 2 reveal
#   POST /pipelines/{id}/stages/{stage_id}/advance   — mark stage complete / submit for approval
#   POST /pipelines/{id}/stages/{stage_id}/approve   — checker approves (maker-checker stages)
#   POST /pipelines/{id}/close                         — decline (bank) or withdraw (borrower), with reason
#   POST /pipelines/{id}/credit-decision               — record a decision made in the bank's own system
#
# Credit approval (db/026): a stage with approval_route=true is decided by the
# approval engine, not by a checker click. Submitting it routes the facility
# through the institution's DoA rules (committees, limits); the engine's
# outcome moves the deal via a DB trigger (approve -> next stage, reject ->
# deal declined). Its decision happens in Inbox > Credit.
#
# Architecture notes:
#   - Fully modular: no cross-module imports outside shared db/deps/config.
#   - All writes use service_session (bypass tenant RLS) so the portal user
#     can advance stages belonging to their institution.
#   - Institution scoping enforced via explicit institution_id guard on every query.
#   - maker-checker: advance -> status='awaiting_approval'; approve -> status='completed' + next stage activates.
# =============================================================================
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import structlog
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core import doa
from ..core.credit_decision import (
    CreditDecisionInput,
    has_decision_permission,
    list_decisions,
    record_decision,
)
from ..core.db import service_session
from ..core.webhooks import dispatch_event
from ..deps import current_claims, require_module, tenant_conn
from .notifications import _write_notification

log = structlog.get_logger()

# Every route below requires the institution to be licensed for the
# "pipeline" module (institution.institution.modules) — a pricing/
# entitlement gate, separate from per-user group RBAC.
router = APIRouter(
    prefix="/pipelines",
    tags=["pipeline"],
    dependencies=[Depends(require_module("pipeline"))],
)


def _documents_outstanding(conn, pipeline_id: str, stage_id: str) -> int:
    """Mandatory checklist documents of this stage not yet verified or waived (db/027)."""
    conn.execute(text("SELECT marketplace.ensure_deal_documents(CAST(:pid AS uuid))"),
                 {"pid": pipeline_id})
    n = conn.execute(text("SELECT marketplace.stage_documents_outstanding(CAST(:sid AS uuid))"),
                     {"sid": stage_id}).scalar_one()
    return int(n or 0)


def documents_gate(conn, pipeline_id: str, stage_id: str) -> None:
    n = _documents_outstanding(conn, pipeline_id, stage_id)
    if n:
        raise HTTPException(
            status_code=409,
            detail=f"{n} required document{'s' if n > 1 else ''} for this stage "
                   f"{'are' if n > 1 else 'is'} not verified or waived yet. See the Documents tab.",
        )


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
             AND psi_cur.status NOT IN ('completed','skipped'))            AS sla_breached,
            -- booked in the bank's core system (db/025)
            EXISTS (SELECT 1 FROM marketplace.loan_booking b
                    WHERE b.pipeline_id = lp.id)                           AS booked
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
            lp.closed_reason,
            lp.closed_at,
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

    # Materialise the document checklist so stage counts are complete.
    with service_session() as svc:
        svc.execute(text("SELECT marketplace.ensure_deal_documents(CAST(:pid AS uuid))"),
                    {"pid": pipeline_id})
        svc.commit()

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
            psd.approval_route,
            psd.decision_source,
            -- document checklist for this stage (db/027)
            (SELECT count(*) FROM marketplace.deal_document dd
               JOIN institution.doc_requirement r ON r.id = dd.requirement_id
              WHERE dd.pipeline_id = psi.pipeline_id AND r.stage_def_id = psi.stage_def_id
                AND r.mandatory)::int                                         AS documents_required,
            (SELECT count(*) FROM marketplace.deal_document dd
               JOIN institution.doc_requirement r ON r.id = dd.requirement_id
              WHERE dd.pipeline_id = psi.pipeline_id AND r.stage_def_id = psi.stage_def_id
                AND r.mandatory AND dd.status IN ('verified', 'waived'))::int AS documents_done,
            psi.approval_instance_id,
            ai.status                         AS approval_status,
            psi.rejection_reason,
            psd.sla_hours,
            -- SLA breach flag
            (psi.sla_due_at IS NOT NULL
             AND psi.sla_due_at < now()
             AND psi.status NOT IN ('completed','skipped'))  AS sla_breached,
            -- documents placeholder (extend when doc uploads are wired in)
            '[]'::jsonb AS documents
        FROM marketplace.pipeline_stage_instance psi
        JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
        LEFT JOIN institution.approval_instance ai ON ai.id = psi.approval_instance_id
        WHERE psi.pipeline_id = :pid
        ORDER BY psi.position
    """), {"pid": pipeline_id}))

    credit_decisions = list_decisions(conn, institution_id, pipeline_id)
    return {**pipeline, "stages": stages, "credit_decisions": credit_decisions}


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
            SELECT psi.id, psi.status, psd.requires_maker_checker, psd.approval_route,
                   psd.decision_source, psd.label, psi.position
            FROM marketplace.pipeline_stage_instance psi
            JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
            WHERE psi.id = :sid AND psi.pipeline_id = :pid
        """), {"sid": stage_id, "pid": pipeline_id}))
        if not stage:
            raise HTTPException(status_code=404, detail="Stage not found.")
        if stage["status"] != "active":
            raise HTTPException(status_code=409,
                detail=f"Stage is '{stage['status']}', not active.")

        documents_gate(conn, pipeline_id, stage_id)

        if stage["approval_route"] and stage["decision_source"] == "external":
            raise HTTPException(status_code=409,
                detail="This product's credit decision is made in your credit system. "
                       "Record the decision on the deal instead.")

        if stage["approval_route"]:
            if not member_id:
                raise HTTPException(status_code=403, detail="Member identity could not be resolved.")
            return _submit_for_credit_approval(
                conn, institution_id, pipeline_id, stage_id, member_id, notes,
            )

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
                link=f"/deals/{pipeline_id}",
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
            SELECT psi.id, psi.status, psi.submitted_by, psi.position, psi.approval_instance_id
            FROM marketplace.pipeline_stage_instance psi
            WHERE psi.id = :sid AND psi.pipeline_id = :pid
        """), {"sid": stage_id, "pid": pipeline_id}))
        if not stage:
            raise HTTPException(status_code=404, detail="Stage not found.")
        if stage["approval_instance_id"]:
            raise HTTPException(status_code=409,
                detail="This stage is decided by credit approval, in Inbox > Credit.")
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
            link=f"/deals/{pipeline_id}",
            metadata={"pipeline_id": pipeline_id, "completed_stage_id": stage_id},
        )
        conn.commit()
        # The borrower hears about it from deal.progress_updated (db/033): no direct call.

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


# ── Credit approval: route a facility through the DoA rules ───────────────────
def _load_facility_rules(conn: Session, institution_id: str) -> list[doa.DoaRule]:
    # Explicit institution filter: this runs on a service session (no RLS).
    rows = conn.execute(text("""
        SELECT id, entity_type, priority, conditions, template_id
        FROM institution.doa_rule
        WHERE institution_id = :iid AND entity_type = 'facility' AND status = 'active'
    """), {"iid": institution_id}).fetchall()
    return [
        doa.DoaRule(
            id=str(r.id), entity_type=r.entity_type, priority=r.priority,
            conditions=r.conditions if isinstance(r.conditions, dict) else json.loads(r.conditions),
            template_id=str(r.template_id),
        )
        for r in rows
    ]


def _submit_for_credit_approval(
    conn: Session, institution_id: str, pipeline_id: str, stage_id: str,
    member_id: str, notes: str | None,
) -> dict:
    deal = _one(conn.execute(text("""
        SELECT lp.deal_amount, lp.deal_rate, lp.deal_term_months,
               UPPER(LEFT(lp.consumer_id::text, 8)) AS consumer_ref,
               cp.code AS product_code, cp.label AS product_label,
               COALESCE(cp.currency, 'MUR') AS currency,
               ba.full_name AS borrower_name
        FROM marketplace.loan_pipeline lp
        LEFT JOIN marketplace.request r ON r.id = lp.request_id
        LEFT JOIN catalog.product cp ON cp.id = r.product_id
        LEFT JOIN marketplace.bid_acceptance ba
               ON ba.request_id = lp.request_id AND ba.institution_id = lp.institution_id
        WHERE lp.id = :pid
    """), {"pid": pipeline_id})) or {}

    params = doa.EntityParams(
        entity_type="facility",
        amount=Decimal(str(deal["deal_amount"])) if deal.get("deal_amount") is not None else None,
        currency=deal.get("currency"), risk_tier=None,
        product_type=deal.get("product_code"), secured=None,
        tenor_months=deal.get("deal_term_months"),
    )
    try:
        rule = doa.evaluate(_load_facility_rules(conn, institution_id), params)
    except doa.NoMatchingRule as exc:
        raise HTTPException(
            status_code=409,
            detail="No credit approval rule covers this facility. "
                   "Add one under Setup > Credit approval.",
        ) from exc

    snapshot = {
        "pipeline_id": pipeline_id,
        "amount": float(deal["deal_amount"]) if deal.get("deal_amount") is not None else None,
        "rate": float(deal["deal_rate"]) if deal.get("deal_rate") is not None else None,
        "term_months": deal.get("deal_term_months"),
        "currency": deal.get("currency"),
        "product_code": deal.get("product_code"),
        "product_label": deal.get("product_label"),
        "borrower_name": deal.get("borrower_name"),
        "consumer_ref": deal.get("consumer_ref"),
        "maker_notes": notes,
    }
    try:
        row = conn.execute(text("""
            SELECT institution.approvals_route(
                CAST(:iid AS uuid), 'facility', CAST(:pid AS uuid), CAST(:maker AS uuid),
                CAST(:snap AS jsonb), CAST(:tpl AS uuid), CAST(:rule AS uuid)) AS id
        """), {
            "iid": institution_id, "pid": pipeline_id, "maker": member_id,
            "snap": json.dumps(snapshot), "tpl": rule.template_id, "rule": rule.id,
        }).fetchone()
    except Exception as exc:  # noqa: BLE001 — engine codes
        conn.rollback()
        if "APPROVALS_TEMPLATE_NOT_ACTIVE" in str(exc):
            raise HTTPException(status_code=409,
                detail="The approval chain this facility routes to is not active.") from exc
        raise
    if row is None:
        raise HTTPException(status_code=500, detail="Internal error.")
    instance_id = str(row.id)

    conn.execute(text("""
        UPDATE marketplace.pipeline_stage_instance
        SET status = 'awaiting_approval', submitted_by = :member, submitted_at = now(),
            notes = COALESCE(:notes, notes), approval_instance_id = CAST(:inst AS uuid),
            rejection_reason = NULL, updated_at = now()
        WHERE id = :sid
    """), {"member": member_id, "notes": notes, "inst": instance_id, "sid": stage_id})
    _write_notification(
        conn, institution_id,
        kind="approval_needed",
        title="Facility submitted for credit approval",
        body="A facility is waiting for a credit decision.",
        link="/inbox?section=credit",
        metadata={"pipeline_id": pipeline_id, "approval_instance_id": instance_id},
    )
    conn.commit()
    return {"status": "awaiting_approval", "stage_id": stage_id, "pipeline_id": pipeline_id,
            "approval_instance_id": instance_id}


# ── POST /pipelines/{id}/close ────────────────────────────────────────────────
_CLOSE_ERRORS: dict[str, tuple[int, str]] = {
    "DEAL_NOT_FOUND": (404, "Deal not found."),
    "DEAL_NOT_ACTIVE": (409, "This deal is already closed."),
    "DEAL_REASON_REQUIRED": (422, "Give a reason (at least 5 characters)."),
    "DEAL_OUTCOME_INVALID": (422, "Outcome must be 'declined' or 'withdrawn'."),
}


@router.post("/{pipeline_id}/close")
async def close_deal(
    pipeline_id: str,
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
) -> dict:
    """
    Close a deal that will not proceed.
      declined  — the bank declines (failed conditions, credit, fraud ...)
      withdrawn — the borrower withdrew
    A reason is mandatory and kept on the deal and in the audit log. Any
    credit approval in progress is withdrawn.
    """
    institution_id = claims.get("institution_id")
    member_id = claims.get("sub")
    if not institution_id or not member_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    outcome = body.get("outcome")
    reason = (body.get("reason") or "").strip()

    with service_session() as conn:
        try:
            result = conn.execute(text("""
                SELECT marketplace.close_deal(
                    CAST(:pid AS uuid), CAST(:iid AS uuid), :outcome, :reason, CAST(:actor AS uuid)
                ) AS r
            """), {"pid": pipeline_id, "iid": institution_id, "outcome": outcome,
                   "reason": reason, "actor": member_id}).scalar_one()
        except Exception as exc:  # noqa: BLE001 — mapped below
            conn.rollback()
            for code, (status, detail) in _CLOSE_ERRORS.items():
                if code in str(exc):
                    raise HTTPException(status_code=status, detail=detail) from exc
            raise
        conn.execute(text("""
            INSERT INTO audit.event
                (id, occurred_at, actor_id, actor_type, actor_email, actor_role,
                 institution_id, action, resource_type, resource_id, outcome, metadata)
            VALUES
                (gen_random_uuid(), now(), CAST(:actor AS uuid), 'member', :email, :role,
                 CAST(:iid AS uuid), :action, 'loan_pipeline', CAST(:pid AS uuid), 'success',
                 CAST(:meta AS jsonb))
        """), {"actor": member_id, "email": claims.get("email"), "role": claims.get("user_role"),
               "iid": institution_id, "action": f"deal.{outcome}", "pid": pipeline_id,
               "meta": json.dumps({"reason": reason})})
        conn.commit()

    asyncio.create_task(dispatch_event(institution_id, "deal.closed", {
        "loan_id": pipeline_id, "outcome": outcome, "reason": reason,
    }))
    return dict(result)


# ── POST /pipelines/{id}/credit-decision ──────────────────────────────────────
@router.post("/{pipeline_id}/credit-decision", status_code=201)
async def record_credit_decision(
    pipeline_id: str,
    background:  BackgroundTasks,
    body:        CreditDecisionInput = Body(...),
    conn:        Session = Depends(tenant_conn),
    claims:      dict = Depends(current_claims),
) -> dict:
    """
    Record a credit decision made in the institution's own system, for a
    product whose credit stage is set to decision_source='external'.
    approved / approved_with_conditions advance the deal; referred_back keeps
    the stage open with the reason; declined declines the deal.
    """
    institution_id = claims.get("institution_id")
    member_id = claims.get("sub")
    if not institution_id or not member_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    # Live group permissions, not the JWT snapshot (same as booking).
    row = conn.execute(text("SELECT modules FROM institution.current_member_ctx_v2()")).first()
    if not has_decision_permission(list(row.modules) if row and row.modules else [],
                                   claims.get("user_role")):
        raise HTTPException(status_code=403, detail="Requires 'inst:approvals'.")
    with service_session() as svc:
        return record_decision(
            svc, institution_id=str(institution_id), pipeline_id=pipeline_id, body=body,
            source="portal", actor_id=member_id, actor_email=claims.get("email"),
            api_key_id=None, background=background,
        )
