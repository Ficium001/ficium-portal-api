# =============================================================================
# ficium-portal-api — Credit decision, irrespective of where it is made
#
# A deal's credit stage (pipeline_stage_def.approval_route) is decided one of
# two ways, set per workflow (so per product) by decision_source:
#
#   internal  Ficium's approval engine — committees, DoA limits (db/026).
#   external  the institution's own credit system. Its decision is recorded
#             here, from either channel:
#               Portal  POST /pipelines/{id}/credit-decision     (inst:approvals)
#               API     POST /v1/pipeline/{id}/credit-decision   (pipeline:write)
#
# Both modes write marketplace.credit_decision, so the deal page and the
# deal.credit_decided webhook read one history. All rules live in
# marketplace.record_credit_decision() (db/029); this module maps its CREDIT_*
# errors to HTTP and runs the side effects after commit.
# =============================================================================
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .webhooks import dispatch_event

log = structlog.get_logger()

MODULE_KEY = "inst:approvals"

Outcome = Literal["approved", "approved_with_conditions", "referred_back", "declined"]


class CreditDecisionInput(BaseModel):
    outcome: Outcome
    external_reference: str = Field(
        ..., min_length=1, max_length=64,
        description="Reference in your credit system (application or minute number).",
    )
    conditions: list[str] = Field(
        default_factory=list, max_length=30,
        description="Required for approved_with_conditions; not allowed otherwise.",
    )
    reason: str | None = Field(
        default=None, max_length=2000,
        description="Required for referred_back and declined (at least 5 characters).",
    )
    decided_at: datetime | None = Field(
        default=None, description="When your credit system decided. Defaults to now.",
    )

    @field_validator("conditions")
    @classmethod
    def _clean_conditions(cls, v: list[str]) -> list[str]:
        cleaned = [c.strip() for c in v if c and c.strip()]
        if any(len(c) > 500 for c in cleaned):
            raise ValueError("Each condition must be at most 500 characters.")
        return cleaned


_ERRORS: dict[str, tuple[int, str]] = {
    "CREDIT_DEAL_NOT_FOUND": (404, "Deal not found."),
    "CREDIT_DEAL_NOT_ACTIVE": (409, "This deal is closed or complete."),
    "CREDIT_STAGE_NOT_ACTIVE": (409, "The deal is not at its credit decision stage."),
    "CREDIT_STAGE_INTERNAL": (
        409, "This product's credit decision is made in Ficium's approval engine, "
             "not recorded from another system."),
    "CREDIT_OUTCOME_INVALID": (422, "Invalid outcome."),
    "CREDIT_REFERENCE_INVALID": (422, "Enter the credit reference (1 to 64 characters)."),
    "CREDIT_CONDITIONS_INVALID": (422, "Conditions must be a list."),
    "CREDIT_CONDITIONS_REQUIRED": (422, "List at least one condition."),
    "CREDIT_CONDITIONS_UNEXPECTED": (
        422, "Conditions are only allowed with approved_with_conditions."),
    "CREDIT_REASON_REQUIRED": (422, "Give a reason (at least 5 characters)."),
    "CREDIT_DATE_INVALID": (
        422, "The decision date must be between the deal start and now."),
    "CREDIT_SOURCE_INVALID": (422, "Invalid decision source."),
}


def _credit_error(exc: DBAPIError) -> HTTPException | None:
    msg = str(getattr(exc, "orig", exc))
    for code, (status, detail) in _ERRORS.items():
        if code in msg:
            return HTTPException(status_code=status, detail=detail)
    return None


def has_decision_permission(modules: list[str] | None, user_role: str | None) -> bool:
    if user_role in ("super_admin", "institution_super_admin"):
        return True
    mods = modules or []
    return "*" in mods or MODULE_KEY in mods


def list_decisions(conn: Session, institution_id: str, pipeline_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("""
            SELECT cd.id, cd.outcome, cd.conditions, cd.reason, cd.external_reference,
                   cd.source, cd.decided_at, cd.created_at, cd.approval_instance_id,
                   portal_admin.get_user_display_name(cd.recorded_by) AS recorded_by_name
              FROM marketplace.credit_decision cd
             WHERE cd.pipeline_id = CAST(:pid AS uuid)
               AND cd.institution_id = CAST(:iid AS uuid)
             ORDER BY cd.created_at DESC
        """),
        {"pid": pipeline_id, "iid": institution_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def record_decision(
    conn: Session,
    *,
    institution_id: str,
    pipeline_id:    str,
    body:           CreditDecisionInput,
    source:         Literal["portal", "api"],
    actor_id:       str | None,
    actor_email:    str | None,
    api_key_id:     str | None,
    background:     BackgroundTasks,
) -> dict[str, Any]:
    """Record an external credit decision; audit in the same transaction;
    commit; then queue webhooks and the borrower notification."""
    try:
        result = conn.execute(
            text("""
                SELECT marketplace.record_credit_decision(
                    CAST(:pid AS uuid), CAST(:iid AS uuid), :outcome, CAST(:conds AS jsonb),
                    :reason, :ref, :decided_at, :source, CAST(:actor AS uuid), CAST(:key AS uuid)
                ) AS r
            """),
            {
                "pid": pipeline_id, "iid": institution_id, "outcome": body.outcome,
                "conds": json.dumps(body.conditions), "reason": body.reason,
                "ref": body.external_reference, "decided_at": body.decided_at,
                "source": source, "actor": actor_id, "key": api_key_id,
            },
        ).scalar_one()
    except DBAPIError as exc:
        conn.rollback()
        mapped = _credit_error(exc)
        if mapped:
            raise mapped from exc
        raise

    conn.execute(
        text("""
            INSERT INTO audit.event
                (id, occurred_at, actor_id, actor_type, actor_email, institution_id,
                 action, resource_type, resource_id, outcome, metadata)
            VALUES
                (gen_random_uuid(), now(), CAST(:actor AS uuid), :actor_type, :email,
                 CAST(:iid AS uuid), 'deal.credit_decided', 'loan_pipeline', CAST(:pid AS uuid),
                 'success', CAST(:meta AS jsonb))
        """),
        {
            "actor": actor_id, "actor_type": "member" if source == "portal" else "api",
            "email": actor_email, "iid": institution_id, "pid": pipeline_id,
            "meta": json.dumps({"source": source, "api_key_id": api_key_id, "decision": result},
                               default=str),
        },
    )
    conn.commit()

    payload = {
        "loan_id":            pipeline_id,
        "outcome":            result["outcome"],
        "conditions":         result["conditions"],
        "reason":             result["reason"],
        "external_reference": result["external_reference"],
        "decided_at":         result["decided_at"],
        "source":             source,
        "deal_status":        result["deal_status"],
    }
    background.add_task(dispatch_event, institution_id, "deal.credit_decided", payload)
    if result["outcome"] == "declined":
        background.add_task(dispatch_event, institution_id, "deal.closed", {
            "loan_id": pipeline_id, "outcome": "declined", "reason": result["reason"],
            "source": "credit_decision",
        })
    # The borrower hears about approvals from deal.progress_updated (db/033): no direct call.
    log.info("credit_decision_recorded", pipeline_id=pipeline_id, source=source,
             outcome=result["outcome"])
    return result
