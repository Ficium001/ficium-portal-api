# =============================================================================
# ficium-portal-api — Approval Engine router (inst:approvals)
#
# Configurable approval chains: committees (quorum), dual control, single
# approver, checklist, external hold — composed per institution, routed by a
# delegation-of-authority matrix (app/core/doa.py). The SAME pure evaluator
# serves POST /doa-rules/simulate and live routing: what you simulate is what
# will happen.
#
# COEXISTS with app/api/approvals.py (governance.action maker-checker) —
# existing dual-control flows are untouched. Prefix is /approval-engine to
# avoid any route collision.
#
# All state transitions run inside SECURITY DEFINER functions installed by
# db/007_approval_engine.sql; this router resolves DoA routing, shapes
# responses, and maps engine error codes to HTTP.
# =============================================================================

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from ..core import doa
from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..deps import current_claims as get_claims
from ..deps import tenant_conn


def _row_or_500(row: Row[Any] | None) -> Row[Any]:
    """Narrow Row | None from INSERT ... RETURNING / SQL function calls."""
    if row is None:
        raise HTTPException(status_code=500, detail="Internal error.")
    return row

router = APIRouter(prefix="/approval-engine", tags=["approval-engine"])

MODULE_KEY = "inst:approvals"

# Engine error code -> (status, user-facing message)
ENGINE_ERRORS: dict[str, tuple[int, str]] = {
    "APPROVALS_MAKER_RECUSED": (409, "Makers cannot approve their own submissions."),
    "APPROVALS_NOT_ELIGIBLE": (403, "You are not an eligible approver for this stage."),
    "APPROVALS_STAGE_NOT_ACTIVE": (409, "This stage is no longer active."),
    "APPROVALS_COMMENT_REQUIRED": (422, "A comment is required for this action."),
    "APPROVALS_DELEGATION_INVALID": (403, "No valid delegation covers this action."),
    "APPROVALS_TEMPLATE_NOT_ACTIVE": (409, "Routed template is not active."),
    "APPROVALS_NOT_WITHDRAWABLE": (409, "Only in-progress approvals can be withdrawn."),
}


def _engine_call(conn: Session, sql: str, params: dict) -> Any:
    """Execute an engine function, mapping engine RAISEs to HTTP errors."""
    try:
        return conn.execute(text(sql), params)
    except Exception as e:  # noqa: BLE001 — engine codes mapped explicitly below
        msg = str(e)
        for code, (status, detail) in ENGINE_ERRORS.items():
            if code in msg:
                conn.rollback()
                raise HTTPException(status_code=status, detail=detail) from e
        if "uq_one_vote_per_actor" in msg or "duplicate key" in msg:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="You have already cast a decisive vote at this stage.",
            ) from e
        raise


def _institution_id(claims: dict) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="No institution context.")
    return iid


def _require_module(claims: dict) -> None:
    role = claims.get("user_role", "")
    if role in INSTITUTION_ADMIN_ROLES:
        return
    if MODULE_KEY not in claims.get("module_permissions", []):
        raise HTTPException(
            status_code=403, detail=f"Requires module permission '{MODULE_KEY}'."
        )


def _require_admin(claims: dict) -> None:
    # Shared with documents.py, institutions.py, marketplace.py — see
    # app/core/roles.py for the live set of role strings ficium-auth
    # actually issues. "institution_admin" (bank-side admins, e.g. MCB)
    # was previously missing from this check entirely.
    if claims.get("user_role", "") not in INSTITUTION_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Requires institution admin.")


def _row(r) -> dict:
    return dict(r._mapping)


# ── Committees ────────────────────────────────────────────────────────────────

@router.get("/committees")
async def list_committees(
    conn: Session = Depends(tenant_conn), claims: dict = Depends(get_claims)
) -> list[dict]:
    _require_module(claims)
    rows = conn.execute(text("""
        SELECT c.*, COALESCE(jsonb_agg(jsonb_build_object(
                 'id', m.id, 'member_id', m.member_id, 'role', m.role,
                 'is_voting', m.is_voting, 'valid_from', m.valid_from,
                 'valid_to', m.valid_to) ORDER BY m.role, m.valid_from)
                 FILTER (WHERE m.id IS NOT NULL), '[]') AS members
        FROM institution.approval_committee c
        LEFT JOIN institution.committee_member m ON m.committee_id = c.id
        GROUP BY c.id ORDER BY c.name
    """)).fetchall()
    return [_row(r) for r in rows]


@router.post("/committees")
async def create_committee(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_admin(claims)
    row = conn.execute(text("""
        INSERT INTO institution.approval_committee
          (institution_id, name, description, quorum_type, quorum_value,
           tie_break, allow_abstain, created_by)
        VALUES (:iid, :name, :description, :quorum_type, :quorum_value,
                COALESCE(:tie_break, 'chair'), COALESCE(:allow_abstain, true), :uid)
        RETURNING id
    """), {
        "iid": _institution_id(claims), "uid": claims["sub"],
        "name": body["name"], "description": body.get("description"),
        "quorum_type": body["quorum_type"], "quorum_value": body.get("quorum_value"),
        "tie_break": body.get("tie_break"), "allow_abstain": body.get("allow_abstain"),
    }).fetchone()
    conn.commit()
    return {"id": str(_row_or_500(row).id)}


@router.post("/committees/{committee_id}/members")
async def add_committee_member(
    committee_id: str,
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_admin(claims)
    row = conn.execute(text("""
        INSERT INTO institution.committee_member
          (committee_id, member_id, role, is_voting, valid_from, valid_to, created_by)
        SELECT :cid, :member_id, COALESCE(:role, 'member'),
               COALESCE(:is_voting, true),
               COALESCE(:valid_from, current_date)::date, :valid_to, :uid
        WHERE EXISTS (SELECT 1 FROM institution.approval_committee WHERE id = :cid)
        RETURNING id
    """), {
        "cid": committee_id, "uid": claims["sub"],
        "member_id": body["member_id"], "role": body.get("role"),
        "is_voting": body.get("is_voting"),
        "valid_from": body.get("valid_from"), "valid_to": body.get("valid_to"),
    }).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Committee not found.")
    conn.commit()
    return {"id": str(row.id)}


@router.delete("/committees/{committee_id}/members/{member_row_id}")
async def end_committee_membership(
    committee_id: str,
    member_row_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_admin(claims)
    conn.execute(text("""
        UPDATE institution.committee_member
        SET valid_to = current_date
        WHERE id = :mid AND committee_id = :cid
    """), {"mid": member_row_id, "cid": committee_id})
    conn.commit()
    return {"ok": True}


# ── Templates ─────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(
    conn: Session = Depends(tenant_conn), claims: dict = Depends(get_claims)
) -> list[dict]:
    _require_module(claims)
    rows = conn.execute(text("""
        SELECT t.*, COALESCE(jsonb_agg(jsonb_build_object(
                 'seq', s.seq, 'name', s.name, 'stage_type', s.stage_type,
                 'committee_id', s.committee_id, 'approver_role', s.approver_role,
                 'sla_hours', s.sla_hours, 'on_sla_breach', s.on_sla_breach)
                 ORDER BY s.seq) FILTER (WHERE s.id IS NOT NULL), '[]') AS stages
        FROM institution.approval_template t
        LEFT JOIN institution.approval_stage_def s ON s.template_id = t.id
        GROUP BY t.id ORDER BY t.name, t.version DESC
    """)).fetchall()
    return [_row(r) for r in rows]


@router.post("/templates")
async def create_template(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_admin(claims)
    stages = body.get("stages") or []
    if not stages:
        raise HTTPException(status_code=422, detail="At least one stage is required.")
    seqs = sorted(s.get("seq", 0) for s in stages)
    if seqs != list(range(1, len(seqs) + 1)):
        raise HTTPException(status_code=422, detail="Stage seq must be contiguous from 1.")

    tpl = _row_or_500(conn.execute(text("""
        INSERT INTO institution.approval_template
          (institution_id, name, entity_type, created_by)
        VALUES (:iid, :name, :entity_type, :uid) RETURNING id
    """), {
        "iid": _institution_id(claims), "uid": claims["sub"],
        "name": body["name"], "entity_type": body["entity_type"],
    }).fetchone())
    for s in stages:
        conn.execute(text("""
            INSERT INTO institution.approval_stage_def
              (template_id, seq, name, stage_type, committee_id, approver_role,
               checklist, sla_hours, on_sla_breach, escalate_to_template_id)
            VALUES (:tid, :seq, :name, :stage_type, :committee_id, :approver_role,
                    CAST(:checklist AS jsonb), :sla_hours,
                    COALESCE(:on_sla_breach, 'notify'), :escalate_to_template_id)
        """), {
            "tid": tpl.id, "seq": s["seq"], "name": s["name"],
            "stage_type": s["stage_type"], "committee_id": s.get("committee_id"),
            "approver_role": s.get("approver_role"),
            "checklist": json.dumps(s["checklist"]) if s.get("checklist") else None,
            "sla_hours": s.get("sla_hours"), "on_sla_breach": s.get("on_sla_breach"),
            "escalate_to_template_id": s.get("escalate_to_template_id"),
        })
    conn.commit()
    return {"id": str(tpl.id), "status": "draft"}


@router.post("/templates/{template_id}/activate")
async def activate_template(
    template_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    """Dual control on governance changes: creator submits (draft ->
    pending_activation); a DIFFERENT admin activates."""
    _require_admin(claims)
    tpl = conn.execute(text("""
        SELECT id, status, created_by FROM institution.approval_template
        WHERE id = :tid
    """), {"tid": template_id}).fetchone()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    if tpl.status == "draft":
        conn.execute(text("""
            UPDATE institution.approval_template
            SET status = 'pending_activation' WHERE id = :tid
        """), {"tid": template_id})
        conn.commit()
        return {"status": "pending_activation"}
    if tpl.status == "pending_activation":
        if str(tpl.created_by) == str(claims["sub"]):
            raise HTTPException(
                status_code=409,
                detail="Activation requires a different admin (dual control).",
            )
        conn.execute(text("""
            UPDATE institution.approval_template
            SET status = 'active', approved_by = :uid WHERE id = :tid
        """), {"tid": template_id, "uid": claims["sub"]})
        conn.commit()
        return {"status": "active"}
    raise HTTPException(status_code=409, detail=f"Template is {tpl.status}.")


# ── DoA rules + simulator ─────────────────────────────────────────────────────

def _load_rules(conn: Session, entity_type: str) -> list[doa.DoaRule]:
    rows = conn.execute(text("""
        SELECT id, entity_type, priority, conditions, template_id
        FROM institution.doa_rule
        WHERE entity_type = :et AND status = 'active'
    """), {"et": entity_type}).fetchall()
    return [
        doa.DoaRule(
            id=str(r.id), entity_type=r.entity_type, priority=r.priority,
            conditions=r.conditions if isinstance(r.conditions, dict)
            else json.loads(r.conditions),
            template_id=str(r.template_id),
        )
        for r in rows
    ]


def _params_from(body: dict) -> doa.EntityParams:
    from decimal import Decimal
    amount = body.get("amount")
    return doa.EntityParams(
        entity_type=body["entity_type"],
        amount=Decimal(str(amount)) if amount is not None else None,
        currency=body.get("currency"), risk_tier=body.get("risk_tier"),
        product_type=body.get("product_type"), secured=body.get("secured"),
        tenor_months=body.get("tenor_months"),
    )


@router.get("/doa-rules")
async def list_doa_rules(
    entity_type: str,
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> list[dict]:
    _require_module(claims)
    rows = conn.execute(text("""
        SELECT r.*, t.name AS template_name
        FROM institution.doa_rule r
        JOIN institution.approval_template t ON t.id = r.template_id
        WHERE r.entity_type = :et AND r.status = 'active'
        ORDER BY r.priority
    """), {"et": entity_type}).fetchall()
    return [_row(r) for r in rows]


@router.post("/doa-rules")
async def create_doa_rule(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_admin(claims)
    priority = int(body["priority"])
    if not (1 <= priority <= 9999):
        raise HTTPException(status_code=422, detail="Priority must be 1..9999.")
    row = conn.execute(text("""
        INSERT INTO institution.doa_rule
          (institution_id, entity_type, priority, conditions, template_id, created_by)
        VALUES (:iid, :entity_type, :priority, CAST(:conditions AS jsonb),
                :template_id, :uid)
        RETURNING id
    """), {
        "iid": _institution_id(claims), "uid": claims["sub"],
        "entity_type": body["entity_type"], "priority": priority,
        "conditions": json.dumps(body.get("conditions") or {}),
        "template_id": body["template_id"],
    }).fetchone()
    conn.commit()
    return {"id": str(_row_or_500(row).id)}


@router.post("/doa-rules/simulate")
async def simulate_routing(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    """Dry-run the DoA matrix. Uses the exact evaluator live routing uses."""
    _require_module(claims)
    try:
        rule = doa.evaluate(_load_rules(conn, body["entity_type"]), _params_from(body))
    except doa.NoMatchingRule as exc:
        raise HTTPException(
            status_code=409,
            detail="No DoA rule matched — the catch-all rule is missing.",
        ) from exc
    tpl = conn.execute(text("""
        SELECT t.id, t.name, t.version,
               jsonb_agg(jsonb_build_object(
                 'seq', s.seq, 'name', s.name, 'stage_type', s.stage_type,
                 'sla_hours', s.sla_hours) ORDER BY s.seq) AS stages
        FROM institution.approval_template t
        JOIN institution.approval_stage_def s ON s.template_id = t.id
        WHERE t.id = :tid GROUP BY t.id
    """), {"tid": rule.template_id}).fetchone()
    return {
        "rule_id": rule.id, "rule_priority": rule.priority,
        "template": _row(tpl) if tpl else None,
    }


# ── Runtime: route / inbox / cast / withdraw / timeline / analytics ───────────

@router.post("/route")
async def route_entity(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_module(claims)
    params = body.get("params") or {"entity_type": body["entity_type"]}
    params["entity_type"] = body["entity_type"]
    try:
        rule = doa.evaluate(_load_rules(conn, body["entity_type"]), _params_from(params))
    except doa.NoMatchingRule as exc:
        raise HTTPException(
            status_code=409,
            detail="No DoA rule matched — the catch-all rule is missing.",
        ) from exc
    row = _engine_call(conn, """
        SELECT institution.approvals_route(
          :iid, :entity_type, :entity_id, :maker,
          CAST(:snapshot AS jsonb), :template_id, :rule_id) AS id
    """, {
        "iid": _institution_id(claims),
        "entity_type": body["entity_type"], "entity_id": body["entity_id"],
        "maker": body.get("entity_maker_id") or claims["sub"],
        "snapshot": json.dumps(body.get("entity_snapshot") or {}),
        "template_id": rule.template_id, "rule_id": rule.id,
    }).fetchone()
    conn.commit()
    return {"instance_id": str(row.id)}


@router.get("/inbox")
async def my_inbox(
    conn: Session = Depends(tenant_conn), claims: dict = Depends(get_claims)
) -> list[dict]:
    _require_module(claims)
    rows = conn.execute(text("""
        WITH active AS (
          SELECT si.id AS stage_instance_id, si.instance_id, si.started_at, si.due_at,
                 sd.name AS stage_name, sd.stage_type, sd.committee_id,
                 i.entity_type, i.entity_snapshot, i.entity_maker_id
          FROM institution.approval_stage_instance si
          JOIN institution.approval_stage_def sd ON sd.id = si.stage_def_id
          JOIN institution.approval_instance i ON i.id = si.instance_id
          WHERE si.status = 'active' AND i.status = 'in_progress'
            AND i.entity_maker_id <> :uid
        )
        SELECT a.*,
          (SELECT count(*) FROM institution.approval_action x
            WHERE x.stage_instance_id = a.stage_instance_id
              AND x.action = 'approve') AS approvals_in,
          (SELECT ac.quorum_value FROM institution.approval_committee ac
            WHERE ac.id = a.committee_id) AS quorum_value,
          (SELECT ac.quorum_type FROM institution.approval_committee ac
            WHERE ac.id = a.committee_id) AS quorum_type,
          (SELECT x.action FROM institution.approval_action x
            WHERE x.stage_instance_id = a.stage_instance_id
              AND COALESCE(x.acting_as, x.actor_id) = :uid
              AND x.action IN ('approve','reject','abstain')) AS my_vote,
          institution.approvals_is_eligible(a.stage_instance_id, :uid) AS eligible
        FROM active a
        ORDER BY a.due_at NULLS LAST, a.started_at
    """), {"uid": claims["sub"]}).fetchall()
    return [_row(r) for r in rows if r.eligible]


@router.post("/instances/{instance_id}/actions")
async def cast_action(
    instance_id: str,
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_module(claims)
    action = body.get("action")
    if action in ("reject", "recuse") and not (body.get("comment") or "").strip():
        raise HTTPException(status_code=422, detail="A comment is required for this action.")
    row = _engine_call(conn, """
        SELECT institution.approvals_cast(
          :sid, :uid, :action, :comment,
          CAST(:checklist AS jsonb), :acting_as) AS status
    """, {
        "sid": body["stage_instance_id"], "uid": claims["sub"],
        "action": action, "comment": body.get("comment"),
        "checklist": json.dumps(body["checklist_state"])
        if body.get("checklist_state") else None,
        "acting_as": body.get("acting_as"),
    }).fetchone()
    conn.commit()
    return {"stage_status": row.status}


@router.post("/instances/{instance_id}/withdraw")
async def withdraw_instance(
    instance_id: str,
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_module(claims)
    if not (body.get("reason") or "").strip():
        raise HTTPException(status_code=422, detail="A reason is required.")
    _engine_call(conn, """
        SELECT institution.approvals_withdraw(:iid_instance, :uid, :reason)
    """, {"iid_instance": instance_id, "uid": claims["sub"], "reason": body["reason"]})
    conn.commit()
    return {"ok": True}


@router.get("/instances/{instance_id}")
async def instance_timeline(
    instance_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_module(claims)
    inst = conn.execute(text("""
        SELECT i.*, t.name AS template_name, r.priority AS rule_priority, r.conditions
        FROM institution.approval_instance i
        JOIN institution.approval_template t ON t.id = i.template_id
        JOIN institution.doa_rule r ON r.id = i.doa_rule_id
        WHERE i.id = :id
    """), {"id": instance_id}).fetchone()
    if inst is None:
        raise HTTPException(status_code=404, detail="Instance not found.")
    stages = conn.execute(text("""
        SELECT si.seq, si.status, si.started_at, si.due_at, si.resolved_at,
               sd.name, sd.stage_type,
               COALESCE(jsonb_agg(jsonb_build_object(
                 'actor_id', a.actor_id, 'acting_as', a.acting_as,
                 'action', a.action, 'comment', a.comment,
                 'created_at', a.created_at) ORDER BY a.created_at)
                 FILTER (WHERE a.id IS NOT NULL), '[]') AS actions
        FROM institution.approval_stage_instance si
        JOIN institution.approval_stage_def sd ON sd.id = si.stage_def_id
        LEFT JOIN institution.approval_action a ON a.stage_instance_id = si.id
        WHERE si.instance_id = :id
        GROUP BY si.id, sd.name, sd.stage_type
        ORDER BY si.seq
    """), {"id": instance_id}).fetchall()
    return {"instance": _row(inst), "stages": [_row(s) for s in stages]}


@router.get("/analytics")
async def approval_analytics(
    conn: Session = Depends(tenant_conn), claims: dict = Depends(get_claims)
) -> dict:
    _require_module(claims)
    cycle = conn.execute(text("""
        SELECT t.name AS template, sd.name AS stage,
               percentile_cont(0.5) WITHIN GROUP
                 (ORDER BY extract(epoch FROM si.resolved_at - si.started_at)/3600)
                 AS median_hours,
               count(*) FILTER (WHERE si.status = 'expired') AS sla_breaches,
               count(*) AS total
        FROM institution.approval_stage_instance si
        JOIN institution.approval_stage_def sd ON sd.id = si.stage_def_id
        JOIN institution.approval_instance i ON i.id = si.instance_id
        JOIN institution.approval_template t ON t.id = i.template_id
        WHERE si.resolved_at IS NOT NULL
        GROUP BY t.name, sd.name ORDER BY median_hours DESC NULLS LAST
    """)).fetchall()
    lost = conn.execute(text("""
        SELECT count(*) AS deals_lost,
               COALESCE(sum((entity_snapshot->>'amount')::numeric), 0) AS amount_lost
        FROM institution.approval_instance
        WHERE status = 'withdrawn' AND withdraw_reason = 'market_expired'
    """)).fetchone()
    return {
        "stage_cycle_times": [_row(r) for r in cycle],
        "lost_in_committee": _row(lost) if lost else {},
    }
