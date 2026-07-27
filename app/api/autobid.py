# =============================================================================
# ficium-portal-api — Auto-Bid Rules router (inst:autobid)
#
# First consumer of the AUTOBID module entitlement (require_module).
# All endpoints JWT-only (portal users): rule governance is a human activity.
#
# Lifecycle (mirrors institution.api_key maker-checker semantics):
#
#   POST /v1/autobid/rules                    create rule + version 1 (draft)
#   POST /v1/autobid/rules/{id}/versions      new immutable version (rule → draft)
#   POST /v1/autobid/rules/{id}/submit        draft → pending_approval (maker stamp)
#   POST /v1/autobid/rules/{id}/approve       checker ≠ maker → active
#   POST /v1/autobid/rules/{id}/reject        pending_approval → rejected (note)
#   POST /v1/autobid/rules/{id}/pause         KILL SWITCH: single actor, instant
#   POST /v1/autobid/rules/{id}/resume        paused → active (version already
#                                             dual-approved; audit-logged)
#   POST /v1/autobid/rules/{id}/retire        terminal
#   GET  /v1/autobid/rules[, /{id}]           list / detail with current version
#   GET  /v1/autobid/executions               audit trail (tenant read via RLS)
#
# Asymmetry is deliberate: STOPPING risk (pause) is single-actor and instant;
# STARTING risk (approve a version) always needs four eyes. Predicates are
# validated against the closed vocabulary in app/core/autobid_rules.py before
# any row is written — the DB freeze trigger then makes submitted versions
# immutable.
# =============================================================================

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.autobid_rules import (
    RuleValidationError,
    validate_action,
    validate_guardrails,
    validate_predicate,
)
from ..core.entitlements import require_module
from ..deps import current_claims, tenant_conn

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/autobid",
    tags=["autobid"],
    dependencies=[Depends(require_module("AUTOBID"))],
)


# ---------------------------------------------------------------------------
# Helpers (same shape as api_keys.py)
# ---------------------------------------------------------------------------

def _iid(claims: dict[str, Any]) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="No institution context.")
    return str(iid)


def _uid(claims: dict[str, Any]) -> str:
    uid = claims.get("sub") or claims.get("user_id")
    if not uid:
        raise HTTPException(status_code=403, detail="Cannot determine user identity.")
    return str(uid)


def _validate_payload(predicate: Any, action: Any, guardrails: Any) -> None:
    try:
        validate_predicate(predicate)
        validate_action(action)
        validate_guardrails(guardrails)
    except RuleValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def _get_rule(conn: Session, iid: str, rule_id: str) -> dict[str, Any]:
    row = conn.execute(
        text("""
            SELECT id, institution_id, name, status, priority, current_version_id,
                   created_by, created_at
            FROM autobid.rule
            WHERE id = CAST(:rid AS uuid) AND institution_id = CAST(:iid AS uuid)
        """),
        {"rid": rule_id, "iid": iid},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found.")
    return dict(row)


def _latest_version(conn: Session, rule_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("""
            SELECT id, version_no, predicate, action, guardrails,
                   submitted_by, submitted_at, approved_by, approved_at,
                   rejected_by, rejected_at, rejection_note, created_at
            FROM autobid.rule_version
            WHERE rule_id = CAST(:rid AS uuid)
            ORDER BY version_no DESC
            LIMIT 1
        """),
        {"rid": rule_id},
    ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateRuleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    priority: int = Field(default=100, ge=1, le=10000)
    predicate: dict[str, Any]
    action: dict[str, Any]
    guardrails: dict[str, Any]


class NewVersionRequest(BaseModel):
    predicate: dict[str, Any]
    action: dict[str, Any]
    guardrails: dict[str, Any]


class RejectRequest(BaseModel):
    note: str = Field(..., min_length=1, max_length=1000)


# ---------------------------------------------------------------------------
# Lifecycle endpoints
# ---------------------------------------------------------------------------

@router.post("/rules", status_code=201)
def create_rule(
    body: Annotated[CreateRuleRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Create a rule with version 1 in draft. Nothing executes until dual-approved."""
    iid, uid = _iid(claims), _uid(claims)
    _validate_payload(body.predicate, body.action, body.guardrails)

    dup = conn.execute(
        text("""
            SELECT 1 FROM autobid.rule
            WHERE institution_id = CAST(:iid AS uuid) AND name = :name
        """),
        {"iid": iid, "name": body.name},
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="A rule with this name already exists.")

    rule = conn.execute(
        text("""
            INSERT INTO autobid.rule
                (institution_id, name, description, status, priority, created_by)
            VALUES
                (CAST(:iid AS uuid), :name, :descr, 'draft', :priority, CAST(:uid AS uuid))
            RETURNING id, name, status, priority, created_at
        """),
        {
            "iid": iid,
            "name": body.name,
            "descr": body.description,
            "priority": body.priority,
            "uid": uid,
        },
    ).mappings().first()
    assert rule is not None

    version = conn.execute(
        text("""
            INSERT INTO autobid.rule_version
                (rule_id, institution_id, version_no, predicate, action, guardrails)
            VALUES
                (CAST(:rid AS uuid), CAST(:iid AS uuid), 1,
                 CAST(:predicate AS jsonb), CAST(:action AS jsonb), CAST(:guardrails AS jsonb))
            RETURNING id, version_no
        """),
        {
            "rid": str(rule["id"]),
            "iid": iid,
            "predicate": _json(body.predicate),
            "action": _json(body.action),
            "guardrails": _json(body.guardrails),
        },
    ).mappings().first()
    assert version is not None

    conn.execute(
        text("UPDATE autobid.rule SET current_version_id = CAST(:vid AS uuid) WHERE id = CAST(:rid AS uuid)"),
        {"vid": str(version["id"]), "rid": str(rule["id"])},
    )
    conn.commit()

    log.info("autobid_rule_created", institution_id=iid, rule_id=str(rule["id"]), by=uid)
    return {**dict(rule), "version_no": version["version_no"], "version_id": str(version["id"])}


@router.post("/rules/{rule_id}/versions", status_code=201)
def new_version(
    rule_id: str,
    body: Annotated[NewVersionRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """
    Create the next immutable version. The rule drops back to draft — the new
    version must pass dual control before it (or the rule) executes again.
    """
    iid = _iid(claims)
    _validate_payload(body.predicate, body.action, body.guardrails)
    rule = _get_rule(conn, iid, rule_id)
    if rule["status"] == "retired":
        raise HTTPException(status_code=409, detail="Rule is retired.")

    version = conn.execute(
        text("""
            INSERT INTO autobid.rule_version
                (rule_id, institution_id, version_no, predicate, action, guardrails)
            SELECT CAST(:rid AS uuid), CAST(:iid AS uuid),
                   COALESCE(MAX(version_no), 0) + 1,
                   CAST(:predicate AS jsonb), CAST(:action AS jsonb), CAST(:guardrails AS jsonb)
            FROM autobid.rule_version WHERE rule_id = CAST(:rid AS uuid)
            RETURNING id, version_no
        """),
        {
            "rid": rule_id,
            "iid": iid,
            "predicate": _json(body.predicate),
            "action": _json(body.action),
            "guardrails": _json(body.guardrails),
        },
    ).mappings().first()
    assert version is not None

    conn.execute(
        text("""
            UPDATE autobid.rule
            SET status = 'draft', current_version_id = CAST(:vid AS uuid), updated_at = now()
            WHERE id = CAST(:rid AS uuid)
        """),
        {"vid": str(version["id"]), "rid": rule_id},
    )
    conn.commit()
    return {"rule_id": rule_id, "version_id": str(version["id"]), "version_no": version["version_no"]}


@router.post("/rules/{rule_id}/submit")
def submit_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Maker submits the current draft version for approval; version freezes."""
    iid, uid = _iid(claims), _uid(claims)
    rule = _get_rule(conn, iid, rule_id)
    if rule["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not draft.")

    conn.execute(
        text("""
            UPDATE autobid.rule_version
            SET submitted_by = CAST(:uid AS uuid), submitted_at = now()
            WHERE id = CAST(:vid AS uuid) AND submitted_at IS NULL
        """),
        {"uid": uid, "vid": str(rule["current_version_id"])},
    )
    conn.execute(
        text("""
            UPDATE autobid.rule SET status = 'pending_approval', updated_at = now()
            WHERE id = CAST(:rid AS uuid)
        """),
        {"rid": rule_id},
    )
    conn.commit()
    log.info("autobid_rule_submitted", institution_id=iid, rule_id=rule_id, maker=uid)
    return {"rule_id": rule_id, "status": "pending_approval"}


@router.post("/rules/{rule_id}/approve")
def approve_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Checker approves — MUST be a different user than the maker. Rule goes live."""
    iid, uid = _iid(claims), _uid(claims)
    rule = _get_rule(conn, iid, rule_id)
    if rule["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not pending_approval.")

    version = conn.execute(
        text("""
            SELECT id, submitted_by FROM autobid.rule_version
            WHERE id = CAST(:vid AS uuid)
        """),
        {"vid": str(rule["current_version_id"])},
    ).mappings().first()
    if version is None or version["submitted_by"] is None:
        raise HTTPException(status_code=409, detail="Current version was never submitted.")
    if str(version["submitted_by"]) == uid:
        raise HTTPException(
            status_code=403,
            detail="Maker-checker: approver must be different from submitter.",
        )

    conn.execute(
        text("""
            UPDATE autobid.rule_version
            SET approved_by = CAST(:uid AS uuid), approved_at = now()
            WHERE id = CAST(:vid AS uuid)
        """),
        {"uid": uid, "vid": str(version["id"])},
    )
    conn.execute(
        text("""
            UPDATE autobid.rule SET status = 'active', updated_at = now()
            WHERE id = CAST(:rid AS uuid)
        """),
        {"rid": rule_id},
    )
    conn.commit()
    log.info("autobid_rule_approved", institution_id=iid, rule_id=rule_id, checker=uid)
    return {"rule_id": rule_id, "status": "active"}


@router.post("/rules/{rule_id}/reject")
def reject_rule(
    rule_id: str,
    body: Annotated[RejectRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    iid, uid = _iid(claims), _uid(claims)
    rule = _get_rule(conn, iid, rule_id)
    if rule["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not pending_approval.")

    conn.execute(
        text("""
            UPDATE autobid.rule_version
            SET rejected_by = CAST(:uid AS uuid), rejected_at = now(), rejection_note = :note
            WHERE id = CAST(:vid AS uuid)
        """),
        {"uid": uid, "vid": str(rule["current_version_id"]), "note": body.note},
    )
    conn.execute(
        text("UPDATE autobid.rule SET status = 'rejected', updated_at = now() WHERE id = CAST(:rid AS uuid)"),
        {"rid": rule_id},
    )
    conn.commit()
    return {"rule_id": rule_id, "status": "rejected"}


@router.post("/rules/{rule_id}/pause")
def pause_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """KILL SWITCH — single actor, instant. Stopping risk must never wait for a second pair of eyes."""
    iid, uid = _iid(claims), _uid(claims)
    rule = _get_rule(conn, iid, rule_id)
    if rule["status"] != "active":
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not active.")
    conn.execute(
        text("UPDATE autobid.rule SET status = 'paused', updated_at = now() WHERE id = CAST(:rid AS uuid)"),
        {"rid": rule_id},
    )
    conn.commit()
    log.warning("autobid_rule_paused", institution_id=iid, rule_id=rule_id, by=uid)
    return {"rule_id": rule_id, "status": "paused"}


@router.post("/rules/{rule_id}/resume")
def resume_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Resume a paused rule. Version is unchanged and already dual-approved; audit-logged."""
    iid, uid = _iid(claims), _uid(claims)
    rule = _get_rule(conn, iid, rule_id)
    if rule["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not paused.")
    conn.execute(
        text("UPDATE autobid.rule SET status = 'active', updated_at = now() WHERE id = CAST(:rid AS uuid)"),
        {"rid": rule_id},
    )
    conn.commit()
    log.warning("autobid_rule_resumed", institution_id=iid, rule_id=rule_id, by=uid)
    return {"rule_id": rule_id, "status": "active"}


@router.post("/rules/{rule_id}/retire")
def retire_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Terminal state. Retired rules never execute and cannot be revived (create a new rule)."""
    iid, uid = _iid(claims), _uid(claims)
    rule = _get_rule(conn, iid, rule_id)
    if rule["status"] == "retired":
        raise HTTPException(status_code=409, detail="Rule already retired.")
    conn.execute(
        text("UPDATE autobid.rule SET status = 'retired', updated_at = now() WHERE id = CAST(:rid AS uuid)"),
        {"rid": rule_id},
    )
    conn.commit()
    log.info("autobid_rule_retired", institution_id=iid, rule_id=rule_id, by=uid)
    return {"rule_id": rule_id, "status": "retired"}


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

@router.get("/rules")
def list_rules(
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
    status: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    iid = _iid(claims)
    where = "WHERE r.institution_id = CAST(:iid AS uuid)"
    params: dict[str, Any] = {"iid": iid}
    if status:
        where += " AND r.status = :status"
        params["status"] = status
    rows = conn.execute(
        text(f"""
            SELECT r.id, r.name, r.description, r.status, r.priority,
                   r.created_at, r.updated_at,
                   v.version_no AS current_version_no,
                   admin.get_user_display_name(v.submitted_by) AS submitted_by_username,
                   admin.get_user_display_name(v.approved_by)  AS approved_by_username
            FROM autobid.rule r
            LEFT JOIN autobid.rule_version v ON v.id = r.current_version_id
            {where}
            ORDER BY r.priority, r.created_at
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/rules/{rule_id}")
def get_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    iid = _iid(claims)
    rule = _get_rule(conn, iid, rule_id)
    version = _latest_version(conn, rule_id)
    return {"rule": rule, "current_version": version}


@router.get("/executions")
def list_executions(
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
    loan_request_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Execution audit trail (worker-written; tenant read isolation via RLS)."""
    iid = _iid(claims)
    where = "WHERE institution_id = CAST(:iid AS uuid)"
    params: dict[str, Any] = {"iid": iid, "limit": limit}
    if loan_request_id:
        where += " AND loan_request_id = CAST(:lrid AS uuid)"
        params["lrid"] = loan_request_id
    rows = conn.execute(
        text(f"""
            SELECT rule_id, rule_version_id, loan_request_id, outcome, bid_id,
                   detail, occurred_at
            FROM autobid.execution
            {where}
            ORDER BY occurred_at DESC
            LIMIT :limit
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    import json

    return json.dumps(obj)
