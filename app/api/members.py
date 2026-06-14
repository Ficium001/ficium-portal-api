# =============================================================================
# ficium-portal-api — Members router
# Replaces: useMyRole, useInstitutionUsers, useMyGroup
# RLS / SECURITY DEFINER scope every read to the caller's identity.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/members", tags=["members"])


def _row_to_dict(row) -> dict:
    return dict(row._mapping)


@router.get("/me")
async def get_my_role(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """The caller's own institution_members row (role, group, flags)."""
    sub = claims.get("sub")
    row = conn.execute(
        text("""
            SELECT *
            FROM institution.institution_members
            WHERE auth_user_id = :uid
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")
    return _row_to_dict(row)


@router.get("/my-group")
async def get_my_group(
    conn: Session = Depends(tenant_conn),
) -> dict | None:
    """
    The caller's group (module_permissions, label, user_type).
    Calls portal_admin.get_my_group() — the SAME SECURITY DEFINER RPC the
    frontend used. Resolves admin_users first, then institution_members,
    via auth.uid(). Returns null when the caller has no group.
    """
    result = conn.execute(text("SELECT portal_admin.get_my_group() AS grp")).fetchone()
    if result is None or result.grp is None:
        return None
    # result.grp is already JSONB → comes back as a dict via psycopg2/SQLAlchemy
    return result.grp


@router.get("")
async def list_members(
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """All active members of the caller's institution (RLS-scoped)."""
    rows = conn.execute(
        text("""
            SELECT *
            FROM institution.institution_members
            WHERE active = true
            ORDER BY created_at
        """)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]
