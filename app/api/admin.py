# =============================================================================
# ficium-portal-api — Admin router (platform administration)
#
# Replaces the Supabase RPCs that previously ran under a Supabase Auth session.
# Now that admins authenticate via ficium-auth (RS256), auth.uid() inside a
# browser->Supabase RPC is null. These endpoints resolve the admin via the
# verified ficium-auth JWT (claims["sub"]) using a service-role DB session.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text

from ..core.db import service_session
from ..deps import current_claims

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(claims: dict, conn) -> str:
    """Verify the caller is an active platform admin. Returns their admin_users.id."""
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="No subject in token.")
    row = conn.execute(
        text("""
            SELECT id FROM portal_admin.admin_users
            WHERE auth_user_id = :uid AND status = 'active'
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=403, detail="Not an active admin.")
    return str(row.id)


@router.get("/me")
async def admin_me(claims: dict = Depends(current_claims)) -> dict | None:
    sub = claims.get("sub")
    with service_session() as conn:
        row = conn.execute(
            text("""
                SELECT to_jsonb(u) AS u FROM portal_admin.admin_users u
                WHERE u.auth_user_id = :uid AND u.status = 'active' LIMIT 1
            """),
            {"uid": sub},
        ).fetchone()
        return row.u if row else None


@router.get("/metrics")
async def admin_metrics(claims: dict = Depends(current_claims)) -> dict:
    with service_session() as conn:
        _require_admin(claims, conn)
        row = conn.execute(
            text("""
                SELECT jsonb_build_object(
                    'total_admins',    (SELECT COUNT(*) FROM portal_admin.admin_users),
                    'active_admins',   (SELECT COUNT(*) FROM portal_admin.admin_users WHERE status = 'active'),
                    'locked_accounts', (SELECT COUNT(*) FROM portal_admin.admin_users WHERE status = 'locked'),
                    'active_sessions', (SELECT COUNT(*) FROM portal_admin.admin_sessions WHERE is_active = TRUE),
                    'pending_dc',      (SELECT COUNT(*) FROM portal_admin.admin_dual_control_actions WHERE status = 'pending'),
                    'recent_audit',    (
                        SELECT jsonb_agg(jsonb_build_object('outcome', outcome))
                        FROM (SELECT outcome FROM portal_admin.admin_audit_log
                              ORDER BY created_at DESC LIMIT 100) sub
                    )
                ) AS m
            """)
        ).fetchone()
        return row.m


@router.get("/users")
async def admin_users(
    status: str | None = Query(default=None),
    claims: dict = Depends(current_claims),
) -> list[dict]:
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT to_jsonb(u) AS u FROM portal_admin.admin_users u
                WHERE :st IS NULL OR u.status = CAST(:st AS portal_admin.admin_user_status)
                ORDER BY u.created_at DESC
            """),
            {"st": status},
        ).fetchall()
        return [r.u for r in rows]


@router.get("/roles")
async def admin_roles(claims: dict = Depends(current_claims)) -> list[dict]:
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT to_jsonb(r) AS r FROM portal_admin.admin_roles r
                ORDER BY r.is_system DESC, r.slug
            """)
        ).fetchall()
        return [r.r for r in rows]


@router.get("/sessions")
async def admin_sessions(
    active_only: bool = Query(default=False),
    claims: dict = Depends(current_claims),
) -> list[dict]:
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT jsonb_build_object(
                    'id', s.id, 'admin_user_id', s.admin_user_id,
                    'ip_address', s.ip_address, 'user_agent', s.user_agent,
                    'country', s.country, 'city', s.city,
                    'started_at', s.started_at, 'last_active_at', s.last_active_at,
                    'ended_at', s.ended_at, 'end_reason', s.end_reason,
                    'is_active', s.is_active, 'admin_email', u.email,
                    'admin_name', u.display_name, 'admin_role', u.role_slug
                ) AS s
                FROM portal_admin.admin_sessions s
                JOIN portal_admin.admin_users u ON u.id = s.admin_user_id
                WHERE NOT :ao OR s.is_active = TRUE
                ORDER BY s.last_active_at DESC
                LIMIT 200
            """),
            {"ao": active_only},
        ).fetchall()
        return [r.s for r in rows]


@router.get("/dual-control")
async def admin_dual_control(
    status: str = Query(default="pending"),
    claims: dict = Depends(current_claims),
) -> list[dict]:
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT to_jsonb(a) AS a FROM portal_admin.admin_dual_control_actions a
                WHERE :st = 'all' OR a.status = CAST(:st AS portal_admin.dual_control_status)
                ORDER BY a.initiated_at DESC
            """),
            {"st": status},
        ).fetchall()
        return [r.a for r in rows]


@router.get("/audit")
async def admin_audit(
    limit: int = Query(default=100, le=500),
    outcome: str | None = Query(default=None),
    category: str | None = Query(default=None),
    claims: dict = Depends(current_claims),
) -> list[dict]:
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT to_jsonb(e) AS e FROM (
                    SELECT * FROM portal_admin.admin_audit_log
                    WHERE (:outcome IS NULL OR outcome = CAST(:outcome AS portal_admin.audit_outcome))
                      AND (:category IS NULL OR action_category ILIKE :category || '%')
                    ORDER BY created_at DESC LIMIT :lim
                ) e
            """),
            {"outcome": outcome, "category": category, "lim": limit},
        ).fetchall()
        return [r.e for r in rows]


@router.get("/institutions")
async def admin_institutions(claims: dict = Depends(current_claims)) -> list[dict]:
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT to_jsonb(i) AS i FROM institution.institution i
                ORDER BY i.created_at DESC
            """)
        ).fetchall()
        return [r.i for r in rows]


@router.get("/user-groups")
async def admin_user_groups(claims: dict = Depends(current_claims)) -> list[dict]:
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT to_jsonb(g) AS g FROM portal_admin.user_groups g
                ORDER BY g.label
            """)
        ).fetchall()
        return [r.g for r in rows]


# =============================================================================
# Dual-control mutations
#
# These call the existing portal_admin.* SECURITY DEFINER RPCs, which resolve
# the admin via auth.uid(). We set request.jwt.claims on the session first so
# auth.uid() returns the ficium-auth sub — reusing all the RPC's built-in
# logic (self-approval blocks, permission checks, execution, audit) without
# reimplementing it in Python.
# =============================================================================

import json as _json

from pydantic import BaseModel
from sqlalchemy import text as _text

from ..core.db import _session_or_raise


def _admin_rpc_session(claims: dict):
    """A session where auth.uid() resolves to the ficium-auth sub."""
    SessionLocal = _session_or_raise()
    claims_json = _json.dumps(claims, separators=(",", ":"))
    session = SessionLocal()
    session.execute(
        _text("SELECT set_config('request.jwt.claims', :c, true)"),
        {"c": claims_json},
    )
    return session


class SubmitDualControlBody(BaseModel):
    action_category: str
    action_label:    str
    risk:            str
    resource_type:   str
    resource_id:     str | None = None
    resource_label:  str | None = None
    payload:         dict = {}
    payload_before:  dict | None = None


@router.post("/dual-control/submit")
async def submit_dual_control(
    body: SubmitDualControlBody,
    claims: dict = Depends(current_claims),
) -> dict:
    session = _admin_rpc_session(claims)
    try:
        _require_admin(claims, session)
        row = session.execute(
            _text("""
                SELECT portal_admin.admin_submit_dual_control(
                    :action_category, :action_label, :risk, :resource_type,
                    :resource_id, :resource_label, :payload, :payload_before
                ) AS action_id
            """),
            {
                "action_category": body.action_category,
                "action_label":    body.action_label,
                "risk":            body.risk,
                "resource_type":   body.resource_type,
                "resource_id":     body.resource_id,
                "resource_label":  body.resource_label,
                "payload":         _json.dumps(body.payload),
                "payload_before":  _json.dumps(body.payload_before) if body.payload_before is not None else None,
            },
        ).fetchone()
        session.commit()
        return {"action_id": str(row.action_id)}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        session.close()


class ApproveBody(BaseModel):
    action_id: str
    note:      str | None = None


@router.post("/dual-control/approve")
async def approve_dual_control(
    body: ApproveBody,
    claims: dict = Depends(current_claims),
) -> dict:
    session = _admin_rpc_session(claims)
    try:
        _require_admin(claims, session)
        session.execute(
            _text("SELECT portal_admin.admin_approve_dual_control(:aid, :note)"),
            {"aid": body.action_id, "note": body.note},
        )
        session.commit()
        return {"ok": True}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        session.close()


class RejectBody(BaseModel):
    action_id: str
    note:      str


@router.post("/dual-control/reject")
async def reject_dual_control(
    body: RejectBody,
    claims: dict = Depends(current_claims),
) -> dict:
    session = _admin_rpc_session(claims)
    try:
        _require_admin(claims, session)
        session.execute(
            _text("SELECT portal_admin.admin_reject_dual_control(:aid, :note)"),
            {"aid": body.action_id, "note": body.note},
        )
        session.commit()
        return {"ok": True}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        session.close()


class TerminateSessionBody(BaseModel):
    session_id: str
    reason:     str


@router.post("/sessions/terminate")
async def terminate_session(
    body: TerminateSessionBody,
    claims: dict = Depends(current_claims),
) -> dict:
    """Force-terminate an admin session and write an audit entry."""
    with service_session() as conn:
        admin_id = _require_admin(claims, conn)
        conn.execute(
            text("""
                UPDATE portal_admin.admin_sessions
                SET is_active = FALSE, ended_at = now(), end_reason = 'forced'
                WHERE id = :sid
            """),
            {"sid": body.session_id},
        )
        conn.execute(
            text("""
                INSERT INTO portal_admin.admin_audit_log
                    (actor_id, action_category, event_label, resource_type, resource_id, outcome, outcome_note)
                VALUES
                    (:aid, 'session.terminate', 'Session forcibly terminated',
                     'admin_session', :sid, 'success', :reason)
            """),
            {"aid": admin_id, "sid": body.session_id, "reason": body.reason},
        )
        return {"ok": True}


# ─── GET /admin/documents ─────────────────────────────────────────────────────
# All institution documents across all institutions — for Ficium admin review.
# Service session: bypasses RLS (admin sees everything).

@router.get("/documents")
async def admin_list_documents(claims: dict = Depends(current_claims)) -> list[dict]:
    """All institution compliance documents with institution info and doc type."""
    with service_session() as conn:
        _require_admin(claims, conn)
        rows = conn.execute(
            text("""
                SELECT
                    d.id,
                    d.institution_id,
                    i.name              AS institution_name,
                    i.institution_type,
                    dt.code             AS doc_type_code,
                    dt.label            AS doc_type_label,
                    dt.is_mandatory,
                    d.file_name,
                    d.storage_path,
                    d.mime_type,
                    d.status,
                    d.expiry_date,
                    d.rejection_reason,
                    d.uploaded_at,
                    d.reviewed_at,
                    m.email             AS uploaded_by_email
                FROM institution.doc d
                JOIN institution.institution i  ON i.id  = d.institution_id
                JOIN institution.doc_type dt    ON dt.id = d.doc_type_id
                LEFT JOIN institution.member m  ON m.id  = d.uploaded_by
                ORDER BY
                    CASE d.status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,
                    d.uploaded_at DESC
            """)
        ).fetchall()
        return [dict(r._mapping) for r in rows]


# ─── POST /admin/documents/{id}/review ────────────────────────────────────────

@router.post("/documents/{doc_id}/review")
async def admin_review_document(
    doc_id: str,
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    """Approve or reject a compliance document. body: { action, rejection_reason? }"""
    action = body.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="action must be 'approve' or 'reject'.")
    if action == "reject" and not body.get("rejection_reason"):
        raise HTTPException(status_code=422, detail="rejection_reason required when rejecting.")

    with service_session() as conn:
        admin_id = _require_admin(claims, conn)
        row = conn.execute(
            text("""
                UPDATE institution.doc
                SET
                    status           = :status,
                    rejection_reason = :reason,
                    reviewed_by      = :reviewer::uuid,
                    reviewed_at      = now()
                WHERE id = :id
                RETURNING id, institution_id, status, rejection_reason, reviewed_at
            """),
            {
                "id":       doc_id,
                "status":   "approved" if action == "approve" else "rejected",
                "reason":   body.get("rejection_reason"),
                "reviewer": admin_id,
            },
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        conn.commit()
        return dict(row._mapping)
