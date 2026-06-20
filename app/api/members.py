# =============================================================================
# ficium-portal-api — Members router (v2 schema)
# Table: institution.member (was institution.institution_members)
# New columns: system_group_id, custom_group_id, member_role,
#              invited_by, invited_at, activated_at, deactivated_at
# Groups: reads admin.system_group (was portal_admin.user_groups)
#         with fallback to portal_admin.user_groups during migration
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/members", tags=["members"])

_ADMIN_ROLES = {"super_admin", "ficium_admin", "platform_admin"}


def _row(row) -> dict:
    return dict(row._mapping)


@router.get("/me")
async def get_my_role(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Caller's institution.member row (v2 table name)."""
    sub       = claims.get("sub")
    user_role = claims.get("user_role", "")
    inst_id   = claims.get("institution_id")

    if user_role in _ADMIN_ROLES:
        return {
            "auth_user_id":    sub,
            "role":            user_role,
            "institution_id":  inst_id,
            "is_primary_admin": True,
            "status":          "active",
        }

    # RLS-scoped via tenant_conn: the member SELECT policies (self +
    # tenant_isolation) ensure the caller resolves only their own row or
    # their own institution's primary admin.
    row = conn.execute(
        text("""
            SELECT *
            FROM institution.member
            WHERE auth_user_id = :uid
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()

    if row is None and inst_id:
        row = conn.execute(
            text("""
                SELECT *
                FROM institution.member
                WHERE institution_id = :iid
                  AND is_primary_admin = true
                LIMIT 1
            """),
            {"iid": inst_id},
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
    Resolve caller's group and module_permissions.
    Priority: admin.system_group → portal_admin.user_groups (compat fallback).
    """
    user_role = claims.get("user_role", "")
    inst_id   = claims.get("institution_id")

    if user_role in _ADMIN_ROLES:
        return {
            "slug":               "super_admin",
            "label":              "Super Admin",
            "description":        "Full platform access — all modules",
            "module_permissions": ["*"],
            "user_type":          "admin",
            "is_system":          True,
        }

    # RLS-scoped via tenant_conn; group templates are readable by authenticated.
    if user_role or inst_id:
        # 1. Try admin.system_group (v2)
        if user_role:
            row = conn.execute(
                text("""
                    SELECT jsonb_build_object(
                        'id',                 id::TEXT,
                        'slug',               slug,
                        'label',              label,
                        'description',        COALESCE(description, ''),
                        'module_permissions', to_jsonb(module_permissions),
                        'user_type',          side,
                        'is_system',          is_system
                    ) AS grp
                    FROM admin.system_group
                    WHERE slug = :slug
                    LIMIT 1
                """),
                {"slug": user_role},
            ).fetchone()
            if row and row.grp:
                return row.grp

        # 2. Fallback to portal_admin.user_groups (compat, pre-migration)
        if user_role:
            row = conn.execute(
                text("""
                    SELECT jsonb_build_object(
                        'id',                 id::TEXT,
                        'slug',               slug,
                        'label',              label,
                        'description',        COALESCE(description, ''),
                        'module_permissions', to_jsonb(module_permissions),
                        'user_type',          'institution',
                        'is_system',          is_system
                    ) AS grp
                    FROM portal_admin.user_groups
                    WHERE slug = :slug
                    LIMIT 1
                """),
                {"slug": user_role},
            ).fetchone()
            if row and row.grp:
                return row.grp

        # 3. institution_id fallback → institution_admin group
        if inst_id:
            row = conn.execute(
                text("""
                    SELECT jsonb_build_object(
                        'id',                 id::TEXT,
                        'slug',               slug,
                        'label',              label,
                        'description',        COALESCE(description, ''),
                        'module_permissions', to_jsonb(module_permissions),
                        'user_type',          side,
                        'is_system',          is_system
                    ) AS grp
                    FROM admin.system_group
                    WHERE slug = 'institution_admin'
                    LIMIT 1
                """),
            ).fetchone()
            if row and row.grp:
                return row.grp

    return None


@router.get("")
async def list_members(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """All active members of the caller's institution (RLS-scoped)."""
    inst_id = claims.get("institution_id")
    if not inst_id:
        return []
    # tenant_isolation RLS policy scopes this to the caller's institution;
    # the explicit institution_id filter is kept as defense-in-depth.
    rows = conn.execute(
        text("""
            SELECT
                m.*,
                COALESCE(sg.slug, pg.slug)               AS group_slug,
                COALESCE(sg.label, pg.label)             AS group_label,
                COALESCE(sg.module_permissions,
                         pg.module_permissions, '{}')    AS module_permissions
            FROM institution.member m
            LEFT JOIN admin.system_group   sg ON sg.id = m.system_group_id
            LEFT JOIN portal_admin.user_groups pg ON pg.id = m.group_id
            WHERE m.institution_id = :iid
              AND m.deactivated_at IS NULL
            ORDER BY m.created_at
        """),
        {"iid": inst_id},
    ).fetchall()
    return [_row(r) for r in rows]
