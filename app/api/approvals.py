# =============================================================================
# ficium-portal-api — Approvals router (v2 schema)
# Table: governance.action (was institution.pending_actions)
# New fields: scope, label, risk, maker_ip, maker_user_agent,
#             resource_label, payload_before, checker_ip, execution_status
# RPC: governance.submit() (was institution.submit_for_approval — shim kept)
# =============================================================================

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import tenant_conn

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _row_to_dict(row) -> dict:
    return dict(row._mapping)


@router.get("/pending")
async def list_pending_actions(
    scope: str = Query(default="institution"),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Pending governance actions for the caller's institution."""
    rows = conn.execute(
        text("""
            SELECT *
            FROM governance.action
            WHERE status = 'pending'
              AND scope  = :scope
            ORDER BY expires_at ASC
        """),
        {"scope": scope},
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/submit")
async def submit_for_approval(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Submit a maker-checker action via governance.submit() RPC.
    body: { action_category, resource_type, resource_id, payload, label?, risk? }
    """
    result = conn.execute(
        text("""
            SELECT governance.submit(
                :cat, :rtype, :rid,
                CAST(:payload AS jsonb),
                :label,
                :risk
            ) AS action_id
        """),
        {
            "cat":     body["action_category"],
            "rtype":   body["resource_type"],
            "rid":     body.get("resource_id"),
            "payload": json.dumps(body.get("payload", {})),
            "label":   body.get("label", ""),
            "risk":    body.get("risk", "medium"),
        },
    ).fetchone()
    return {"action_id": str(result.action_id)}


@router.post("/{action_id}/approve")
async def approve_action(
    action_id: str,
    body: dict = Body(default={}),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Approve a pending governance action (checker only — enforced in RPC)."""
    try:
        result = conn.execute(
            text("SELECT institution.approve_action(:aid, :note) AS res"),
            {"aid": action_id, "note": body.get("note")},
        ).fetchone()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"result": result.res if result else None}


@router.post("/{action_id}/reject")
async def reject_action(
    action_id: str,
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Reject a pending governance action with a required note."""
    note = body.get("note")
    if not note:
        raise HTTPException(status_code=422, detail="A rejection note is required.")
    try:
        result = conn.execute(
            text("SELECT institution.reject_action(:aid, :note) AS res"),
            {"aid": action_id, "note": note},
        ).fetchone()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"result": result.res if result else None}
