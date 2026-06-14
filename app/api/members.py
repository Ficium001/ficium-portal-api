# =============================================================================
# ficium-portal-api — Members router
# Replaces: useMyRole, useInstitutionUsers (institution_members queries)
# RLS scopes every read to the caller's institution.
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


@router.get("")
async def list_members(
    claims: dict = Depends(current_claims),
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
