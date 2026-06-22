# =============================================================================
# ficium-portal-api — Members router
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/members", tags=["members"])


def _row(row) -> dict:
    return dict(row._mapping)


@router.get("/me")
async def get_my_role(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """The caller's own institution_members row."""
    sub = claims.get("sub")
    row = conn.execute(
        text("""
            SELECT *
            FROM institution.member
            WHERE auth_user_id = :uid
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")
    return _row(row)


@router.get("/my-group")
async def get_my_group(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict | None:
    """
    Resolve the caller's group and module_permissions.
    Uses sub from the verified JWT directly (avoids auth.uid() inside
    SECURITY DEFINER which may return null in pgbouncer transaction mode).
    Priority: custom institution group > platform system group.
    """
    sub = claims.get("sub")
    if not sub:
        return None

    # 0. Admin path — platform admins live in admin.user, not institution.member.
    #    Their modules come from admin.system_group (super_admin → ["*"]).
    if claims.get("user_role") in ("admin", "super_admin"):
        row = conn.execute(
            text("""
                SELECT jsonb_build_object(
                    'id',                 sg.id::TEXT,
                    'slug',               sg.slug,
                    'label',              sg.label,
                    'description',        COALESCE(sg.description, ''),
                    'module_permissions', to_jsonb(sg.module_permissions),
                    'user_type',          'admin',
                    'is_system',          true
                ) AS grp
                FROM  admin."user" u
                JOIN  admin.system_group sg ON sg.id = u.system_group_id
                WHERE u.auth_user_id = :uid
                LIMIT 1
            """),
            {"uid": sub},
        ).fetchone()
        if row and row.grp:
            return row.grp
        return None

    # 1. Custom institution group
    row = conn.execute(
        text("""
            SELECT jsonb_build_object(
                'id',                 g.id::TEXT,
                'slug',               g.slug,
                'label',              g.label,
                'description',        COALESCE(g.description, ''),
                'module_permissions', to_jsonb(g.module_permissions),
                'user_type',          'institution',
                'is_system',          g.is_system
            ) AS grp
            FROM  institution.member im
            JOIN  institution.group g ON g.id = im.custom_group_id
            WHERE im.auth_user_id = :uid
              AND im.active       = true
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row and row.grp:
        return row.grp

    # 2. Platform system group fallback (group_id -> portal_admin.user_groups)
    row = conn.execute(
        text("""
            SELECT jsonb_build_object(
                'id',                 ug.id::TEXT,
                'slug',               ug.slug,
                'label',              ug.label,
                'description',        COALESCE(ug.description, ''),
                'module_permissions', to_jsonb(ug.module_permissions),
                'user_type',          'institution',
                'is_system',          ug.is_system
            ) AS grp
            FROM  institution.member im
            JOIN  portal_admin.user_groups ug ON ug.id = im.group_id
            WHERE im.auth_user_id = :uid
              AND im.active       = true
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row and row.grp:
        return row.grp

    return None


@router.get("/my-group-debug")
async def get_my_group_debug(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Temporary: returns raw member row to confirm sub + active state."""
    sub = claims.get("sub")
    row = conn.execute(
        text("""
            SELECT id, auth_user_id, active, group_id, custom_group_id, is_primary_admin
            FROM institution.member
            WHERE auth_user_id = :uid
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    return {"sub": sub, "member": dict(row._mapping) if row else None}


@router.get("")
async def list_members(
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """All active members of the caller's institution (RLS-scoped)."""
    rows = conn.execute(
        text("""
            SELECT *
            FROM institution.member
            WHERE active = true
            ORDER BY created_at
        """)
    ).fetchall()
    return [_row(r) for r in rows]
