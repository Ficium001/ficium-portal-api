# =============================================================================
# ficium-portal-api — Members router
# =============================================================================
#
# Auth note: ficium-auth uses its OWN user UUIDs as JWT sub, which differ
# from Supabase auth.users UUIDs stored in institution_members.auth_user_id.
# We therefore resolve group/modules from JWT CLAIMS directly:
#
#   JWT claim `user_role`     -> maps to portal_admin.user_groups slug
#   JWT claim `institution_id` -> which institution the caller belongs to
#
# Priority for group resolution:
#   1. super_admin / ficium_admin  -> hardcoded ['*'] admin group
#   2. user_role slug match        -> portal_admin.user_groups WHERE slug = user_role
#   3. institution_id present      -> fallback to 'institution_admin' group
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import current_claims, tenant_conn
from ..core.db import service_session

router = APIRouter(prefix="/members", tags=["members"])

_ADMIN_ROLES = {"super_admin", "ficium_admin", "platform_admin"}


def _row(row) -> dict:
    return dict(row._mapping)


@router.get("/me")
async def get_my_role(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Returns the caller's institution_members row if they are an
    institution user, or a synthetic row for Ficium admin users.
    """
    sub       = claims.get("sub")
    user_role = claims.get("user_role", "")
    inst_id   = claims.get("institution_id")

    # Ficium admins may not have an institution_members row
    if user_role in _ADMIN_ROLES:
        return {
            "auth_user_id": sub,
            "role": user_role,
            "institution_id": inst_id,
            "is_primary_admin": True,
            "active": True,
        }

    # Institution user — look up by sub first, then by institution_id fallback
    with service_session() as svc:
        row = svc.execute(
            text("""
                SELECT *
                FROM institution.institution_members
                WHERE auth_user_id = :uid
                LIMIT 1
            """),
            {"uid": sub},
        ).fetchone()

        if row is None and inst_id:
            # ficium-auth sub != supabase UUID; find by institution + primary admin
            row = svc.execute(
                text("""
                    SELECT *
                    FROM institution.institution_members
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
    Resolve the caller's group and module_permissions from JWT claims.
    ficium-auth sub != Supabase UUID so we resolve by user_role + institution_id.
    """
    user_role = claims.get("user_role", "")
    inst_id   = claims.get("institution_id")

    # 1. Ficium / platform admins get full access
    if user_role in _ADMIN_ROLES:
        return {
            "slug":               "super_admin",
            "label":              "Super Admin",
            "description":        "Full platform access — all modules",
            "module_permissions": ["*"],
            "user_type":          "admin",
            "is_system":          True,
        }

    # 2. Map user_role slug -> portal_admin.user_groups
    with service_session() as svc:
        if user_role:
            row = svc.execute(
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

        # 3. Fallback: institution_id present -> institution_admin group
        if inst_id:
            row = svc.execute(
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
) -> list[dict]:
    """All active members of the caller's institution."""
    inst_id = claims.get("institution_id")
    if not inst_id:
        return []
    with service_session() as svc:
        rows = svc.execute(
            text("""
                SELECT *
                FROM institution.institution_members
                WHERE institution_id = :iid
                  AND active = true
                ORDER BY created_at
            """),
            {"iid": inst_id},
        ).fetchall()
    return [_row(r) for r in rows]
