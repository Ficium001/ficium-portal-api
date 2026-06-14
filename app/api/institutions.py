# =============================================================================
# ficium-portal-api — Institutions router
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/institutions", tags=["institutions"])


@router.get("/me")
async def get_my_institution(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Return the caller's institution gate status: approved / suspended / pending.
    Admin users (no institution_id in token) are passed through directly.
    """
    institution_id = claims.get("institution_id")

    if not institution_id:
        if claims.get("user_role") == "admin":
            return {"user_type": "admin", "approved": True,
                    "suspended_at": None, "suspension_reason": None}
        raise HTTPException(status_code=403, detail="No institution context.")

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
        "user_type":          "institution",
        "institution_id":     institution_id,
        "approved":           row.approved,
        "suspended_at":       row.suspended_at.isoformat() if row.suspended_at else None,
        "suspension_reason":  row.suspension_reason,
    }
