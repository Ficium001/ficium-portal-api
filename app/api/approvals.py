# =============================================================================
# ficium-portal-api — Approvals router (maker-checker core)
# Replaces: usePendingActions, useSubmitBid, useApproveAction, useRejectAction
#
# These call the SAME SECURITY DEFINER RPCs the frontend used on Supabase:
#   submit_for_approval(p_action_category, p_resource_type, p_resource_id, p_payload)
#   approve_action(p_action_id, p_note)
#   reject_action(p_action_id, p_note)
# Dual-control enforcement lives in those functions — unchanged.
# =============================================================================

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import tenant_conn

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _row_to_dict(row) -> dict:
    return dict(row._mapping)


@router.get("/pending")
async def list_pending_actions(
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Pending maker-checker actions for the caller's institution."""
    rows = conn.execute(
        text("""
            SELECT *
            FROM institution.pending_actions
            WHERE action_status = 'pending'
            ORDER BY expires_at ASC
        """)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/submit")
async def submit_for_approval(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Submit an action for dual-control approval.
    body: { action_category, resource_type, resource_id, payload }
    """
    result = conn.execute(
        text("""
            SELECT institution.submit_for_approval(
                :cat, :rtype, :rid, CAST(:payload AS jsonb)
            ) AS action_id
        """),
        {
            "cat":   body["action_category"],
            "rtype": body["resource_type"],
            "rid":   body.get("resource_id"),
            "payload": json.dumps(body.get("payload", {})),
        },
    ).fetchone()
    if result is None or result.action_id is None:
        raise HTTPException(status_code=500, detail="submit_for_approval returned no id.")
    return {"action_id": str(result.action_id)}


@router.post("/{action_id}/approve")
async def approve_action(
    action_id: str,
    body: dict = Body(default={}),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Approve a pending action (institution admins only — enforced in RPC)."""
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
    """Reject a pending action with a required note."""
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
