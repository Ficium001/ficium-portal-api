# =============================================================================
# ficium-portal-api — Institutions router
# Stage 3 slice: the institution-status check that PortalRoute needs to gate
# the Portal. Replaces the browser's direct Supabase query
#   supabase.schema('institution').from('institutions').select(...).eq('id', instId)
# Identity comes from the verified token; RLS scopes the row to the caller.
# =============================================================================

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/institutions", tags=["institutions"])


@router.get("/me")
async def get_my_institution(
    claims: dict = Depends(current_claims),
    conn: asyncpg.Connection = Depends(tenant_conn),
) -> dict:
    """
    Return the caller's institution gate status: approved / suspended / pending.
    Mirrors what PortalRoute consumed from Supabase, but the API resolves the
    institution from the token's institution_id and RLS guarantees the caller
    can only read their own institution row.
    """
    institution_id = claims.get("institution_id")

    # Ficium admins (no institution) are gated by role, not by an institution row.
    if not institution_id:
        if claims.get("user_role") == "admin":
            return {"kind": "admin", "status": "ok"}
        raise HTTPException(status_code=403, detail="No institution context.")

    row = await conn.fetchrow(
        """
        SELECT approved, suspended_at, suspension_reason
        FROM institution.institutions
        WHERE id = $1
        """,
        institution_id,
    )

    if row is None:
        # RLS returned nothing → not the caller's institution, or missing.
        raise HTTPException(status_code=404, detail="Institution not found.")

    if row["suspended_at"] is not None:
        return {
            "kind": "institution",
            "status": "suspended",
            "suspension_reason": row["suspension_reason"],
        }
    if not row["approved"]:
        return {"kind": "institution", "status": "pending"}

    return {"kind": "institution", "status": "ok"}
