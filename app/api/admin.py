# =============================================================================
# ficium-portal-api — Admin router (platform administration)
#
# Replaces the Supabase RPCs that previously ran under a Supabase Auth session.
# Now that admins authenticate via ficium-auth (RS256), auth.uid() inside a
# browser->Supabase RPC is null. These endpoints resolve the admin via the
# verified ficium-auth JWT (claims["sub"]) using a service-role DB session.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
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
                WHERE :st IS NULL OR u.status = :st::portal_admin.admin_user_status
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
                WHERE :st = 'all' OR a.status = :st::portal_admin.dual_control_status
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
                    WHERE (:outcome IS NULL OR outcome = :outcome::portal_admin.audit_outcome)
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
