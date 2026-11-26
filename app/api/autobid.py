# =============================================================================
# ficium-portal-api — Autobid (inst:autobid), licensed per bank (AUTOBID).
#
# Rules (Ficium-hosted decisioning), lifecycle mirrors maker-checker elsewhere:
#   POST /v1/autobid/rules                    create rule + version 1 (draft)
#   POST /v1/autobid/rules/{id}/versions      new immutable version (rule -> draft)
#   POST /v1/autobid/rules/{id}/submit        draft -> pending_approval (maker)
#   POST /v1/autobid/rules/{id}/approve       a different checker/admin -> active
#   POST /v1/autobid/rules/{id}/reject        pending_approval -> rejected (note)
#   POST /v1/autobid/rules/{id}/pause         KILL SWITCH: one person, instant
#   POST /v1/autobid/rules/{id}/resume        paused -> active (version already approved)
#   POST /v1/autobid/rules/{id}/retire        terminal
#   POST /v1/autobid/rules/preview            how a draft rule would have bid on recent requests
#   GET  /v1/autobid/rules[, /{id}]
#
# How the bank runs autobid (its own process), every change four-eyes:
#   GET  /v1/autobid/config                   live setting, pending change, vocabulary
#   POST /v1/autobid/config/changes           ask for a change (with a reason)
#   POST /v1/autobid/config/changes/{id}/approve | reject | cancel
#   GET  /v1/autobid/config/changes           history
#   POST /v1/autobid/config/pause             KILL SWITCH for the whole bank: one person, instant
#   POST /v1/autobid/config/engine/secret     new signing secret for the bank's engine (admin; shown once)
#   POST /v1/autobid/config/engine/test       send the engine a sample request, see its answer
#
# What happened:
#   GET  /v1/autobid/executions               every evaluation, including "no rule matched"
#   GET  /v1/autobid/summary                  today: outcomes, caps used
#
# Asymmetry is deliberate: STOPPING risk (pause) is one person and instant;
# STARTING risk (a rule version, switching on, a new mode) always needs four eyes.
# Every actor id stored here is institution.member.id (never the ficium-auth subject).
# =============================================================================

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core import autobid_decision
from ..core.autobid_facts import REQUEST_SQL, build_facts
from ..core.autobid_rules import (
    PRODUCT_TYPES,
    RuleValidationError,
    matches,
    price,
    validate_action,
    validate_guardrails,
    validate_predicate,
    vocabulary,
)
from ..core.db import service_session
from ..core.entitlements import require_module
from ..core.ssrf import WebhookUrlError, validate_webhook_url
from ..deps import current_claims, tenant_conn

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/autobid",
    tags=["autobid"],
    dependencies=[Depends(require_module("AUTOBID"))],
)

MODULE_KEY = "inst:autobid"


# ---------------------------------------------------------------------------
# Who is calling
# ---------------------------------------------------------------------------


class Member:
    def __init__(self, row: Any, claims: dict[str, Any]) -> None:
        self.id = str(row.member_id)
        self.institution_id = str(row.institution_id)
        self.is_admin = bool(row.is_admin)
        self.role = row.member_role or "maker"
        self.modules = list(row.modules or [])
        self.claims = claims

    @property
    def may_use(self) -> bool:
        return self.is_admin or "*" in self.modules or MODULE_KEY in self.modules

    @property
    def may_approve(self) -> bool:
        return self.may_use and (self.is_admin or self.role == "checker")


def _member(conn: Session, claims: dict[str, Any]) -> Member:
    if not claims.get("institution_id"):
        raise HTTPException(status_code=403, detail="No institution context.")
    row = conn.execute(
        text(
            "SELECT member_id, institution_id, is_admin, member_role, modules "
            "FROM institution.current_member_ctx_v2()"
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=403, detail="Not an active member of an institution.")
    m = Member(row, claims)
    if not m.may_use:
        raise HTTPException(status_code=403, detail=f"Requires '{MODULE_KEY}'.")
    return m


def _approver(m: Member, maker_id: str | None) -> None:
    if not m.may_approve:
        raise HTTPException(
            status_code=403, detail="Only an approver (checker) or an admin can approve."
        )
    if maker_id and str(maker_id) == m.id:
        raise HTTPException(
            status_code=403, detail="Four eyes: someone else must approve your own request."
        )


def _audit(
    m: Member, action: str, resource_type: str, resource_id: str | None, metadata: dict[str, Any]
) -> None:
    with service_session() as svc:
        svc.execute(
            text("""
            INSERT INTO audit.event
                (id, occurred_at, actor_id, actor_type, actor_email, actor_role, institution_id,
                 action, resource_type, resource_id, outcome, metadata)
            VALUES (gen_random_uuid(), now(), CAST(:actor AS uuid), 'member', :email, :role,
                    CAST(:iid AS uuid), :action, :rtype, CAST(:rid AS uuid), 'success',
                    CAST(:meta AS jsonb))
        """),
            {
                "actor": m.claims.get("sub"),
                "email": m.claims.get("email"),
                "role": m.claims.get("user_role"),
                "iid": m.institution_id,
                "action": action,
                "rtype": resource_type,
                "rid": resource_id,
                "meta": json.dumps(metadata, default=str),
            },
        )


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _validate_payload(predicate: Any, action: Any, guardrails: Any) -> None:
    try:
        validate_predicate(predicate)
        validate_action(action)
        validate_guardrails(guardrails)
    except RuleValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Rules
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


class NoteRequest(BaseModel):
    note: str = Field(..., min_length=1, max_length=1000)


class PreviewRequest(BaseModel):
    predicate: dict[str, Any]
    action: dict[str, Any]
    days: int = Field(default=30, ge=1, le=90)


def _get_rule(conn: Session, iid: str, rule_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            text("""
        SELECT id, institution_id, name, description, status, priority, current_version_id,
               created_by, created_at, updated_at
          FROM autobid.rule
         WHERE id = CAST(:rid AS uuid) AND institution_id = CAST(:iid AS uuid)
    """),
            {"rid": rule_id, "iid": iid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found.")
    return dict(row)


def _current_version(conn: Session, version_id: Any) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text("""
        SELECT v.id, v.version_no, v.predicate, v.action, v.guardrails,
               v.submitted_by, v.submitted_at, v.approved_by, v.approved_at,
               v.rejected_by, v.rejected_at, v.rejection_note, v.created_at,
               ms.full_name AS submitted_by_name, ma.full_name AS approved_by_name
          FROM autobid.rule_version v
          LEFT JOIN institution.member ms ON ms.id = v.submitted_by
          LEFT JOIN institution.member ma ON ma.id = v.approved_by
         WHERE v.id = CAST(:vid AS uuid)
    """),
            {"vid": str(version_id)},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _set_status(conn: Session, rule_id: str, status: str) -> None:
    conn.execute(
        text(
            "UPDATE autobid.rule SET status = :s, updated_at = now() WHERE id = CAST(:rid AS uuid)"
        ),
        {"s": status, "rid": rule_id},
    )


@router.post("/rules", status_code=201)
def create_rule(
    body: Annotated[CreateRuleRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Create a rule with version 1 in draft. Nothing runs until a second person approves."""
    m = _member(conn, claims)
    _validate_payload(body.predicate, body.action, body.guardrails)
    if conn.execute(
        text("SELECT 1 FROM autobid.rule WHERE institution_id = CAST(:iid AS uuid) AND name = :n"),
        {"iid": m.institution_id, "n": body.name},
    ).first():
        raise HTTPException(status_code=409, detail="A rule with this name already exists.")
    rule = (
        conn.execute(
            text("""
        INSERT INTO autobid.rule (institution_id, name, description, status, priority, created_by)
        VALUES (CAST(:iid AS uuid), :name, :descr, 'draft', :priority, CAST(:mid AS uuid))
        RETURNING id, name, status, priority, created_at
    """),
            {
                "iid": m.institution_id,
                "name": body.name,
                "descr": body.description,
                "priority": body.priority,
                "mid": m.id,
            },
        )
        .mappings()
        .first()
    )
    assert rule is not None
    version = (
        conn.execute(
            text("""
        INSERT INTO autobid.rule_version (rule_id, institution_id, version_no, predicate, action, guardrails)
        VALUES (CAST(:rid AS uuid), CAST(:iid AS uuid), 1,
                CAST(:p AS jsonb), CAST(:a AS jsonb), CAST(:g AS jsonb))
        RETURNING id, version_no
    """),
            {
                "rid": str(rule["id"]),
                "iid": m.institution_id,
                "p": _json(body.predicate),
                "a": _json(body.action),
                "g": _json(body.guardrails),
            },
        )
        .mappings()
        .first()
    )
    assert version is not None
    conn.execute(
        text(
            "UPDATE autobid.rule SET current_version_id = CAST(:v AS uuid) WHERE id = CAST(:r AS uuid)"
        ),
        {"v": str(version["id"]), "r": str(rule["id"])},
    )
    conn.commit()
    log.info(
        "autobid_rule_created", institution_id=m.institution_id, rule_id=str(rule["id"]), by=m.id
    )
    return {**dict(rule), "version_no": version["version_no"], "version_id": str(version["id"])}


@router.post("/rules/{rule_id}/versions", status_code=201)
def new_version(
    rule_id: str,
    body: Annotated[NewVersionRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Next immutable version; the rule drops to draft until the new version is approved."""
    m = _member(conn, claims)
    _validate_payload(body.predicate, body.action, body.guardrails)
    rule = _get_rule(conn, m.institution_id, rule_id)
    if rule["status"] == "retired":
        raise HTTPException(status_code=409, detail="Rule is retired.")
    version = (
        conn.execute(
            text("""
        INSERT INTO autobid.rule_version (rule_id, institution_id, version_no, predicate, action, guardrails)
        SELECT CAST(:rid AS uuid), CAST(:iid AS uuid), COALESCE(MAX(version_no), 0) + 1,
               CAST(:p AS jsonb), CAST(:a AS jsonb), CAST(:g AS jsonb)
          FROM autobid.rule_version WHERE rule_id = CAST(:rid AS uuid)
        RETURNING id, version_no
    """),
            {
                "rid": rule_id,
                "iid": m.institution_id,
                "p": _json(body.predicate),
                "a": _json(body.action),
                "g": _json(body.guardrails),
            },
        )
        .mappings()
        .first()
    )
    assert version is not None
    conn.execute(
        text("""
        UPDATE autobid.rule SET status = 'draft', current_version_id = CAST(:v AS uuid), updated_at = now()
         WHERE id = CAST(:r AS uuid)
    """),
        {"v": str(version["id"]), "r": rule_id},
    )
    conn.commit()
    return {
        "rule_id": rule_id,
        "version_id": str(version["id"]),
        "version_no": version["version_no"],
    }


@router.post("/rules/{rule_id}/submit")
def submit_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Maker submits the draft version for approval; the version freezes."""
    m = _member(conn, claims)
    rule = _get_rule(conn, m.institution_id, rule_id)
    if rule["status"] not in ("draft", "rejected"):
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not draft.")
    v = _current_version(conn, rule["current_version_id"])
    if v is None or v["submitted_at"] is not None:
        raise HTTPException(
            status_code=409, detail="Make a new version to change a submitted rule."
        )
    conn.execute(
        text("""
        UPDATE autobid.rule_version SET submitted_by = CAST(:mid AS uuid), submitted_at = now()
         WHERE id = CAST(:vid AS uuid) AND submitted_at IS NULL
    """),
        {"mid": m.id, "vid": str(v["id"])},
    )
    _set_status(conn, rule_id, "pending_approval")
    conn.commit()
    return {"rule_id": rule_id, "status": "pending_approval"}


@router.post("/rules/{rule_id}/approve")
def approve_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """A different checker or admin approves; the rule goes live."""
    m = _member(conn, claims)
    rule = _get_rule(conn, m.institution_id, rule_id)
    if rule["status"] != "pending_approval":
        raise HTTPException(
            status_code=409, detail=f"Rule is {rule['status']}, not pending approval."
        )
    v = _current_version(conn, rule["current_version_id"])
    if v is None or v["submitted_by"] is None:
        raise HTTPException(status_code=409, detail="Current version was never submitted.")
    _approver(m, str(v["submitted_by"]))
    conn.execute(
        text("""
        UPDATE autobid.rule_version SET approved_by = CAST(:mid AS uuid), approved_at = now()
         WHERE id = CAST(:vid AS uuid)
    """),
        {"mid": m.id, "vid": str(v["id"])},
    )
    _set_status(conn, rule_id, "active")
    conn.commit()
    _audit(
        m,
        "autobid.rule_approved",
        "autobid_rule",
        rule_id,
        {"version_no": v["version_no"], "maker": str(v["submitted_by"])},
    )
    return {"rule_id": rule_id, "status": "active"}


@router.post("/rules/{rule_id}/reject")
def reject_rule(
    rule_id: str,
    body: Annotated[NoteRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    m = _member(conn, claims)
    rule = _get_rule(conn, m.institution_id, rule_id)
    if rule["status"] != "pending_approval":
        raise HTTPException(
            status_code=409, detail=f"Rule is {rule['status']}, not pending approval."
        )
    v = _current_version(conn, rule["current_version_id"])
    _approver(m, str(v["submitted_by"]) if v else None)
    conn.execute(
        text("""
        UPDATE autobid.rule_version
           SET rejected_by = CAST(:mid AS uuid), rejected_at = now(), rejection_note = :note
         WHERE id = CAST(:vid AS uuid)
    """),
        {"mid": m.id, "vid": str(rule["current_version_id"]), "note": body.note},
    )
    _set_status(conn, rule_id, "rejected")
    conn.commit()
    return {"rule_id": rule_id, "status": "rejected"}


@router.post("/rules/{rule_id}/pause")
def pause_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """KILL SWITCH: one person, instant. Stopping risk never waits for a second pair of eyes."""
    m = _member(conn, claims)
    rule = _get_rule(conn, m.institution_id, rule_id)
    if rule["status"] != "active":
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not active.")
    _set_status(conn, rule_id, "paused")
    conn.commit()
    _audit(m, "autobid.rule_paused", "autobid_rule", rule_id, {})
    return {"rule_id": rule_id, "status": "paused"}


@router.post("/rules/{rule_id}/resume")
def resume_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Resume a paused rule: same version, already approved by two people."""
    m = _member(conn, claims)
    rule = _get_rule(conn, m.institution_id, rule_id)
    if rule["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"Rule is {rule['status']}, not paused.")
    _set_status(conn, rule_id, "active")
    conn.commit()
    _audit(m, "autobid.rule_resumed", "autobid_rule", rule_id, {})
    return {"rule_id": rule_id, "status": "active"}


@router.post("/rules/{rule_id}/retire")
def retire_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Terminal: a retired rule never runs again (create a new one)."""
    m = _member(conn, claims)
    rule = _get_rule(conn, m.institution_id, rule_id)
    if rule["status"] == "retired":
        raise HTTPException(status_code=409, detail="Rule already retired.")
    _set_status(conn, rule_id, "retired")
    conn.commit()
    return {"rule_id": rule_id, "status": "retired"}


@router.post("/rules/preview")
def preview_rule(
    body: Annotated[PreviewRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """How this rule would have bid on the requests this bank saw recently. Nothing is placed."""
    _member(conn, claims)
    try:
        validate_predicate(body.predicate)
        validate_action(body.action)
    except RuleValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    rows = (
        conn.execute(
            text(
                REQUEST_SQL + " WHERE r.created_at > now() - make_interval(days => :d) "
                "ORDER BY r.created_at DESC LIMIT 500"
            ),
            {"d": body.days},
        )
        .mappings()
        .all()
    )
    sample: list[dict[str, Any]] = []
    matched, total_amount = 0, 0.0
    for r in rows:
        facts = build_facts(dict(r))
        if not matches(body.predicate, facts):
            continue
        matched += 1
        bid = price(body.action, facts)
        if bid:
            total_amount += bid.amount
        if len(sample) < 20:
            sample.append(
                {
                    "request_id": str(r["id"]),
                    "product": facts.get("product"),
                    "amount": facts.get("amount"),
                    "term_months": facts.get("term_months"),
                    "risk_tier": facts.get("risk_tier"),
                    "bid": bid.__dict__ if bid else None,
                }
            )
    return {
        "days": body.days,
        "requests_checked": len(rows),
        "would_bid": matched,
        "total_amount": round(total_amount, 2),
        "sample": sample,
    }


@router.get("/rules")
def list_rules(
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
    status: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    m = _member(conn, claims)
    rows = (
        conn.execute(
            text("""
        SELECT r.id, r.name, r.description, r.status, r.priority, r.created_at, r.updated_at,
               v.version_no AS current_version_no, v.submitted_by, v.approved_at,
               ms.full_name AS submitted_by_name, ma.full_name AS approved_by_name,
               v.action, v.guardrails
          FROM autobid.rule r
          LEFT JOIN autobid.rule_version v ON v.id = r.current_version_id
          LEFT JOIN institution.member ms ON ms.id = v.submitted_by
          LEFT JOIN institution.member ma ON ma.id = v.approved_by
         WHERE r.institution_id = CAST(:iid AS uuid)
           AND (CAST(:status AS text) IS NULL OR r.status = CAST(:status AS text))
         ORDER BY CASE r.status WHEN 'retired' THEN 1 ELSE 0 END, r.priority, r.created_at
    """),
            {"iid": m.institution_id, "status": status},
        )
        .mappings()
        .all()
    )
    return [
        {**dict(r), "mine": str(r["submitted_by"]) == m.id if r["submitted_by"] else False}
        for r in rows
    ]


@router.get("/rules/{rule_id}")
def get_rule(
    rule_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    m = _member(conn, claims)
    rule = _get_rule(conn, m.institution_id, rule_id)
    version = (
        _current_version(conn, rule["current_version_id"]) if rule["current_version_id"] else None
    )
    mine = bool(version and version["submitted_by"] and str(version["submitted_by"]) == m.id)
    return {
        "rule": rule,
        "current_version": version,
        "mine": mine,
        "can_approve": m.may_approve and not mine,
    }


# ---------------------------------------------------------------------------
# Per-bank configuration (four eyes)
# ---------------------------------------------------------------------------

_CONFIG_COLS = (
    "enabled",
    "decision_mode",
    "approval_mode",
    "engine_url",
    "engine_timeout_ms",
    "engine_failure_mode",
    "max_bids_per_day",
    "max_amount_per_day",
    "product_codes",
)
_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "decision_mode": "rules",
    "approval_mode": "queue_for_approval",
    "engine_url": None,
    "engine_timeout_ms": 5000,
    "engine_failure_mode": "skip",
    "max_bids_per_day": 20,
    "max_amount_per_day": 10_000_000.0,
    "product_codes": None,
}


class ConfigChangeRequest(BaseModel):
    proposed: dict[str, Any]
    reason: str = Field(..., min_length=3, max_length=500)


def _live_config(conn: Session, iid: str) -> dict[str, Any]:
    row = (
        conn.execute(
            text("""
        SELECT enabled, decision_mode, approval_mode, engine_url, engine_timeout_ms,
               engine_failure_mode, max_bids_per_day, max_amount_per_day, product_codes,
               engine_secret_set_at, version, changed_by, approved_by, updated_at
          FROM autobid.institution_config WHERE institution_id = CAST(:iid AS uuid)
    """),
            {"iid": iid},
        )
        .mappings()
        .first()
    )
    if row is None:
        return {
            **_DEFAULTS,
            "engine_secret_set_at": None,
            "version": 0,
            "changed_by": None,
            "approved_by": None,
            "updated_at": None,
        }
    out = dict(row)
    out["max_amount_per_day"] = float(out["max_amount_per_day"])
    return out


def validate_config(merged: dict[str, Any]) -> None:
    """Raise HTTPException(422) unless the full configuration is coherent."""

    def bad(msg: str) -> None:
        raise HTTPException(status_code=422, detail=msg)

    if not isinstance(merged["enabled"], bool):
        bad("enabled must be true or false")
    if merged["decision_mode"] not in ("rules", "external", "rules_then_external"):
        bad("decision_mode must be rules, external or rules_then_external")
    if merged["approval_mode"] not in ("queue_for_approval", "pre_approved"):
        bad("approval_mode must be queue_for_approval or pre_approved")
    if merged["engine_failure_mode"] not in ("skip", "use_rule"):
        bad("engine_failure_mode must be skip or use_rule")
    if (
        merged["engine_failure_mode"] == "use_rule"
        and merged["decision_mode"] != "rules_then_external"
    ):
        bad("use_rule as a fallback needs decision_mode rules_then_external")
    t = merged["engine_timeout_ms"]
    if not isinstance(t, int) or isinstance(t, bool) or not 500 <= t <= 10000:
        bad("engine_timeout_ms must be whole milliseconds from 500 to 10000")
    b = merged["max_bids_per_day"]
    if not isinstance(b, int) or isinstance(b, bool) or not 1 <= b <= 10000:
        bad("max_bids_per_day must be a whole number from 1 to 10000")
    a = merged["max_amount_per_day"]
    if not isinstance(a, (int, float)) or isinstance(a, bool) or a <= 0:
        bad("max_amount_per_day must be above 0")
    pc = merged["product_codes"]
    if pc is not None:
        if not isinstance(pc, list) or not pc or any(p not in PRODUCT_TYPES for p in pc):
            bad("product_codes must be a non-empty list of known products, or null for all")
    url = merged["engine_url"]
    if merged["decision_mode"] != "rules":
        if not url:
            bad("this decision mode needs the address of your decision engine (engine_url)")
    if url is not None:
        if not isinstance(url, str) or len(url) > 2000:
            bad("engine_url must be an https address")
        try:
            validate_webhook_url(url)
        except WebhookUrlError as e:
            bad(f"engine_url refused: {e}")


@router.get("/config")
def get_config(
    conn: Session = Depends(tenant_conn), claims: dict[str, Any] = Depends(current_claims)
) -> dict[str, Any]:
    m = _member(conn, claims)
    live = _live_config(conn, m.institution_id)
    pending = (
        conn.execute(
            text("""
        SELECT c.id, c.proposed, c.before, c.reason, c.proposed_by, c.created_at, c.expires_at,
               mp.full_name AS proposed_by_name
          FROM autobid.config_change c
          LEFT JOIN institution.member mp ON mp.id = c.proposed_by
         WHERE c.institution_id = CAST(:iid AS uuid) AND c.status = 'pending' AND c.expires_at > now()
    """),
            {"iid": m.institution_id},
        )
        .mappings()
        .first()
    )
    active_rules = conn.execute(
        text(
            "SELECT count(*) FROM autobid.rule WHERE institution_id = CAST(:iid AS uuid) AND status = 'active'"
        ),
        {"iid": m.institution_id},
    ).scalar()
    p = dict(pending) if pending else None
    if p:
        p["mine"] = str(p["proposed_by"]) == m.id
        p["can_approve"] = m.may_approve and not p["mine"]
    return {
        "config": live,
        "pending_change": p,
        "active_rules": active_rules,
        "me": {"member_id": m.id, "may_approve": m.may_approve, "is_admin": m.is_admin},
        "vocabulary": vocabulary(),
        "decision_contract": autobid_decision.CONTRACT,
    }


@router.post("/config/changes", status_code=201)
def propose_config(
    body: Annotated[ConfigChangeRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """Ask for a change to how this bank runs autobid. A second person approves it."""
    m = _member(conn, claims)
    unknown = set(body.proposed) - set(_CONFIG_COLS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown settings: {sorted(unknown)}")
    live = _live_config(conn, m.institution_id)
    before = {k: live[k] for k in _CONFIG_COLS}
    merged = {**before, **body.proposed}
    validate_config(merged)
    if merged == before:
        raise HTTPException(status_code=422, detail="Nothing would change.")
    with service_session() as svc:
        svc.execute(
            text("""
            UPDATE autobid.config_change SET status = 'expired'
             WHERE institution_id = CAST(:iid AS uuid) AND status = 'pending' AND expires_at <= now()
        """),
            {"iid": m.institution_id},
        )
        if svc.execute(
            text("""
            SELECT 1 FROM autobid.config_change
             WHERE institution_id = CAST(:iid AS uuid) AND status = 'pending'
        """),
            {"iid": m.institution_id},
        ).first():
            raise HTTPException(status_code=409, detail="A change is already waiting for approval.")
        row = (
            svc.execute(
                text("""
            INSERT INTO autobid.config_change (institution_id, proposed, before, reason, proposed_by)
            VALUES (CAST(:iid AS uuid), CAST(:p AS jsonb), CAST(:b AS jsonb), :reason, CAST(:mid AS uuid))
            RETURNING id, status, created_at
        """),
                {
                    "iid": m.institution_id,
                    "p": _json(merged),
                    "b": _json(before),
                    "reason": body.reason,
                    "mid": m.id,
                },
            )
            .mappings()
            .first()
        )
    assert row is not None
    _audit(
        m,
        "autobid.config_requested",
        "autobid_config",
        str(row["id"]),
        {
            "changes": {k: merged[k] for k in _CONFIG_COLS if merged[k] != before[k]},
            "reason": body.reason,
        },
    )
    return dict(row)


def _pending(svc: Session, iid: str, change_id: str) -> dict[str, Any]:
    row = (
        svc.execute(
            text("""
        SELECT * FROM autobid.config_change
         WHERE id = CAST(:id AS uuid) AND institution_id = CAST(:iid AS uuid) FOR UPDATE
    """),
            {"id": change_id, "iid": iid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Change not found.")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"This change is {row['status']}.")
    if row["expires_at"] <= datetime.now(UTC):
        svc.execute(
            text(
                "UPDATE autobid.config_change SET status = 'expired' WHERE id = CAST(:id AS uuid)"
            ),
            {"id": change_id},
        )
        svc.commit()
        raise HTTPException(status_code=409, detail="This change expired. Ask again.")
    return dict(row)


@router.post("/config/changes/{change_id}/approve")
def approve_config(
    change_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """A different approver applies the change. A new engine gets a signing secret, shown here once."""
    m = _member(conn, claims)
    new_secret: str | None = None
    with service_session() as svc:
        ch = _pending(svc, m.institution_id, change_id)
        _approver(m, str(ch["proposed_by"]))
        p = ch["proposed"]
        live = (
            svc.execute(
                text("""
            SELECT enabled, decision_mode, approval_mode, engine_url, engine_timeout_ms,
                   engine_failure_mode, max_bids_per_day, max_amount_per_day, product_codes,
                   engine_signing_secret
              FROM autobid.institution_config
             WHERE institution_id = CAST(:iid AS uuid) FOR UPDATE
        """),
                {"iid": m.institution_id},
            )
            .mappings()
            .first()
        )
        now_cfg = (
            {k: (float(live[k]) if k == "max_amount_per_day" else live[k]) for k in _CONFIG_COLS}
            if live
            else dict(_DEFAULTS)
        )
        asked_on = {
            k: (float(ch["before"][k]) if k == "max_amount_per_day" else ch["before"].get(k))
            for k in _CONFIG_COLS
        }
        if now_cfg != asked_on:
            raise HTTPException(
                status_code=409,
                detail="The setting changed since this was asked (e.g. paused). Ask again.",
            )
        secret = live["engine_signing_secret"] if live else None
        if p.get("engine_url") and (not secret or (live and live["engine_url"] != p["engine_url"])):
            secret = new_secret = "abs_" + secrets.token_hex(32)
        svc.execute(
            text("""
            INSERT INTO autobid.institution_config
                (institution_id, enabled, decision_mode, approval_mode, engine_url, engine_signing_secret,
                 engine_secret_set_at, engine_timeout_ms, engine_failure_mode, max_bids_per_day,
                 max_amount_per_day, product_codes, version, changed_by, approved_by, updated_at)
            VALUES (CAST(:iid AS uuid), :enabled, :dm, :am, :url, :secret,
                    CASE WHEN CAST(:new AS boolean) THEN now() END, :tmo, :fm, :mbd, :mad,
                    CAST(:pc AS text[]), 1, CAST(:maker AS uuid), CAST(:checker AS uuid), now())
            ON CONFLICT (institution_id) DO UPDATE SET
                enabled = EXCLUDED.enabled, decision_mode = EXCLUDED.decision_mode,
                approval_mode = EXCLUDED.approval_mode, engine_url = EXCLUDED.engine_url,
                engine_signing_secret = EXCLUDED.engine_signing_secret,
                engine_secret_set_at = CASE WHEN CAST(:new AS boolean) THEN now()
                                            ELSE autobid.institution_config.engine_secret_set_at END,
                engine_timeout_ms = EXCLUDED.engine_timeout_ms,
                engine_failure_mode = EXCLUDED.engine_failure_mode,
                max_bids_per_day = EXCLUDED.max_bids_per_day, max_amount_per_day = EXCLUDED.max_amount_per_day,
                product_codes = EXCLUDED.product_codes, version = autobid.institution_config.version + 1,
                changed_by = EXCLUDED.changed_by, approved_by = EXCLUDED.approved_by, updated_at = now()
        """),
            {
                "iid": m.institution_id,
                "enabled": p["enabled"],
                "dm": p["decision_mode"],
                "am": p["approval_mode"],
                "url": p.get("engine_url"),
                "secret": secret if p.get("engine_url") else None,
                "new": new_secret is not None,
                "tmo": p["engine_timeout_ms"],
                "fm": p["engine_failure_mode"],
                "mbd": p["max_bids_per_day"],
                "mad": p["max_amount_per_day"],
                "pc": p.get("product_codes"),
                "maker": str(ch["proposed_by"]),
                "checker": m.id,
            },
        )
        svc.execute(
            text("""
            UPDATE autobid.config_change
               SET status = 'approved', decided_by = CAST(:mid AS uuid), decided_at = now()
             WHERE id = CAST(:id AS uuid)
        """),
            {"mid": m.id, "id": change_id},
        )
    _audit(
        m,
        "autobid.config_approved",
        "autobid_config",
        change_id,
        {
            "maker": str(ch["proposed_by"]),
            "enabled": p["enabled"],
            "decision_mode": p["decision_mode"],
            "approval_mode": p["approval_mode"],
            "new_engine_secret": new_secret is not None,
        },
    )
    out: dict[str, Any] = {"id": change_id, "status": "approved"}
    if new_secret:
        out["engine_signing_secret"] = new_secret
        out["warning"] = (
            "Shown once. Give it to whoever runs your decision engine; it verifies "
            "the Ficium-Signature header on every call."
        )
    return out


@router.post("/config/changes/{change_id}/reject")
def reject_config(
    change_id: str,
    body: Annotated[NoteRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    m = _member(conn, claims)
    with service_session() as svc:
        ch = _pending(svc, m.institution_id, change_id)
        _approver(m, str(ch["proposed_by"]))
        svc.execute(
            text("""
            UPDATE autobid.config_change SET status = 'rejected', decided_by = CAST(:mid AS uuid),
                   decided_at = now(), decision_note = :note
             WHERE id = CAST(:id AS uuid)
        """),
            {"mid": m.id, "id": change_id, "note": body.note},
        )
    _audit(m, "autobid.config_rejected", "autobid_config", change_id, {"note": body.note})
    return {"id": change_id, "status": "rejected"}


@router.post("/config/changes/{change_id}/cancel")
def cancel_config(
    change_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    m = _member(conn, claims)
    with service_session() as svc:
        ch = _pending(svc, m.institution_id, change_id)
        if str(ch["proposed_by"]) != m.id:
            raise HTTPException(status_code=403, detail="Only the person who asked can cancel.")
        svc.execute(
            text(
                "UPDATE autobid.config_change SET status = 'cancelled' WHERE id = CAST(:id AS uuid)"
            ),
            {"id": change_id},
        )
    return {"id": change_id, "status": "cancelled"}


@router.get("/config/changes")
def list_config_changes(
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    m = _member(conn, claims)
    rows = (
        conn.execute(
            text("""
        SELECT c.id, c.proposed, c.before, c.reason, c.status, c.created_at, c.decided_at, c.decision_note,
               mp.full_name AS proposed_by_name, md.full_name AS decided_by_name
          FROM autobid.config_change c
          LEFT JOIN institution.member mp ON mp.id = c.proposed_by
          LEFT JOIN institution.member md ON md.id = c.decided_by
         WHERE c.institution_id = CAST(:iid AS uuid)
         ORDER BY c.created_at DESC LIMIT :lim
    """),
            {"iid": m.institution_id, "lim": limit},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.post("/config/pause")
def pause_bank(
    conn: Session = Depends(tenant_conn), claims: dict[str, Any] = Depends(current_claims)
) -> dict[str, Any]:
    """KILL SWITCH for the whole bank: autobid off now, one person. Turning it back on is four eyes."""
    m = _member(conn, claims)
    with service_session() as svc:
        res = svc.execute(
            text("""
            UPDATE autobid.institution_config
               SET enabled = false, version = version + 1, updated_at = now()
             WHERE institution_id = CAST(:iid AS uuid) AND enabled
        """),
            {"iid": m.institution_id},
        )
        n = getattr(res, "rowcount", 0)
    if n:
        _audit(m, "autobid.paused", "autobid_config", None, {})
        log.warning("autobid_bank_paused", institution_id=m.institution_id, by=m.id)
    return {"enabled": False}


@router.post("/config/engine/secret")
def rotate_engine_secret(
    conn: Session = Depends(tenant_conn), claims: dict[str, Any] = Depends(current_claims)
) -> dict[str, Any]:
    """New signing secret for the bank's decision engine (admin only). The old one stops at once."""
    m = _member(conn, claims)
    if not m.is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can replace the signing secret.")
    new_secret = "abs_" + secrets.token_hex(32)
    with service_session() as svc:
        res = svc.execute(
            text("""
            UPDATE autobid.institution_config
               SET engine_signing_secret = :s, engine_secret_set_at = now(), updated_at = now()
             WHERE institution_id = CAST(:iid AS uuid) AND engine_url IS NOT NULL
        """),
            {"s": new_secret, "iid": m.institution_id},
        )
        n = getattr(res, "rowcount", 0)
    if not n:
        raise HTTPException(status_code=409, detail="Set up a decision engine address first.")
    _audit(m, "autobid.engine_secret_rotated", "autobid_config", None, {})
    return {
        "engine_signing_secret": new_secret,
        "warning": "Shown once. Update your decision engine now; calls signed with the old secret stop.",
    }


_SAMPLE_REQUEST: dict[str, Any] = {
    "request_id": "00000000-0000-4000-8000-000000000000",
    "product_type": "personal_loan",
    "status": "open",
    "bid_window_closes_at": None,
    "created_at": None,
    "allocation_mode": None,
    "facts": {
        "product": "personal_loan",
        "amount": 500000,
        "term_months": 60,
        "currency": "MUR",
        "risk_tier": "B",
        "ficium_score": 71,
        "kyc_verified": True,
        "income_verified": True,
        "employment_status": "employed",
        "gross_monthly_income": 85000,
        "dsr_post_pct": 32,
        "age": 38,
    },
}


@router.post("/config/engine/test")
def test_engine(
    conn: Session = Depends(tenant_conn), claims: dict[str, Any] = Depends(current_claims)
) -> dict[str, Any]:
    """Send the bank's engine a clearly-marked sample request and show its answer. Nothing is placed."""
    m = _member(conn, claims)
    with service_session() as svc:
        cfg = (
            svc.execute(
                text("""
            SELECT engine_url, engine_signing_secret, engine_timeout_ms FROM autobid.institution_config
             WHERE institution_id = CAST(:iid AS uuid)
        """),
                {"iid": m.institution_id},
            )
            .mappings()
            .first()
        )
    if not cfg or not cfg["engine_url"] or not cfg["engine_signing_secret"]:
        raise HTTPException(status_code=409, detail="No decision engine is set up yet.")
    answer = autobid_decision.ask(
        cfg["engine_url"],
        cfg["engine_signing_secret"],
        int(cfg["engine_timeout_ms"]),
        m.institution_id,
        _SAMPLE_REQUEST,
        None,
        event="autobid.decision_test",
    )
    return {"answer": answer.__dict__, "sample_request": _SAMPLE_REQUEST}


# ---------------------------------------------------------------------------
# What happened
# ---------------------------------------------------------------------------


@router.get("/executions")
def list_executions(
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
    outcome: str | None = Query(default=None, max_length=40),
    loan_request_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Every evaluation (worker-written; RLS keeps each bank to its own)."""
    m = _member(conn, claims)
    rows = (
        conn.execute(
            text("""
        SELECT e.id, e.rule_id, e.rule_version_id, e.loan_request_id, e.outcome, e.bid_id,
               e.detail, e.occurred_at, r.name AS rule_name
          FROM autobid.execution e
          LEFT JOIN autobid.rule r ON r.id = e.rule_id
         WHERE e.institution_id = CAST(:iid AS uuid)
           AND (CAST(:outcome AS text) IS NULL OR e.outcome = CAST(:outcome AS text))
           AND (CAST(:lrid AS uuid) IS NULL OR e.loan_request_id = CAST(:lrid AS uuid))
         ORDER BY e.occurred_at DESC LIMIT :lim
    """),
            {"iid": m.institution_id, "outcome": outcome, "lrid": loan_request_id, "lim": limit},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/summary")
def summary(
    conn: Session = Depends(tenant_conn), claims: dict[str, Any] = Depends(current_claims)
) -> dict[str, Any]:
    m = _member(conn, claims)
    by_outcome = {
        r.outcome: r.n
        for r in conn.execute(
            text("""
        SELECT outcome, count(*) AS n FROM autobid.execution
         WHERE institution_id = CAST(:iid AS uuid) AND occurred_at >= date_trunc('day', now())
         GROUP BY outcome
    """),
            {"iid": m.institution_id},
        ).fetchall()
    }
    exp = (
        conn.execute(
            text("""
        SELECT CASE WHEN today = current_date THEN bids_today ELSE 0 END AS bids_today,
               CASE WHEN today = current_date THEN principal_today_mur ELSE 0 END AS amount_today
          FROM autobid.exposure WHERE institution_id = CAST(:iid AS uuid)
    """),
            {"iid": m.institution_id},
        )
        .mappings()
        .first()
    )
    live = _live_config(conn, m.institution_id)
    return {
        "today": by_outcome,
        "bids_today": int(exp["bids_today"]) if exp else 0,
        "amount_today": float(exp["amount_today"]) if exp else 0.0,
        "max_bids_per_day": live["max_bids_per_day"],
        "max_amount_per_day": live["max_amount_per_day"],
        "enabled": live["enabled"],
    }
