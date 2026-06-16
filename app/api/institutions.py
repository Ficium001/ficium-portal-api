# =============================================================================
# ficium-portal-api — Institutions router
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from ..deps import current_claims

router = APIRouter(prefix="/institutions", tags=["institutions"])

_ADMIN_ROLES = ("admin", "super_admin")


@router.get("/me")
async def get_my_institution(
    claims: dict = Depends(current_claims),
) -> dict:
    """
    Return the caller's institution gate status: approved / suspended / pending.

    Admins are gated by role, not by an institution row — handled first so the
    request never opens a tenant DB session for them (admins legitimately may
    not have a real institution row even if a placeholder institution_id is in
    the token).
    """
    if claims.get("user_role") in _ADMIN_ROLES:
        return {
            "user_type":         "admin",
            "institution_id":    None,
            "approved":          True,
            "suspended_at":      None,
            "suspension_reason": None,
        }

    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    # Open a tenant-scoped session only for institution users.
    from ..core.db import tenant_session
    with tenant_session(claims) as conn:
        row = conn.execute(
            text("""
                SELECT approved, suspended_at, suspension_reason
                FROM institution.institutions
                WHERE id = :id
            """),
            {"id": institution_id},
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Institution not found.")

    return {
        "user_type":         "institution",
        "institution_id":    institution_id,
        "approved":          row.approved,
        "suspended_at":      row.suspended_at.isoformat() if row.suspended_at else None,
        "suspension_reason": row.suspension_reason,
    }
