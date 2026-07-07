# =============================================================================
# ficium-portal-api — Institutions router
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from ..deps import current_claims

router = APIRouter(prefix="/institutions", tags=["institutions"])

# Platform-only: Ficium's own internal staff, who have no institution_id.
# Deliberately excludes "institution_admin" — institution-side admins (e.g.
# MCB) must fall through to the real institution lookup below, not this stub.
_ADMIN_ROLES = ("admin", "super_admin")


@router.get("/me")
async def get_my_institution(
    claims: dict = Depends(current_claims),
) -> dict:
    """
    Return the caller's institution profile + gate status.

    Admins get a minimal stub (no real institution row).
    Institution users get the full profile needed by the portal settings page.
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

    from ..core.db import tenant_session
    with tenant_session(claims) as conn:
        row = conn.execute(
            text("""
                SELECT
                    id,
                    name,
                    legal_name,
                    institution_type,
                    reg_number,
                    country,
                    regulator,
                    website,
                    deployment_model,
                    modules,
                    onboarding_stage,
                    compliance_status,
                    compliance_notes,
                    approved,
                    approved_at,
                    suspended_at,
                    suspension_reason,
                    primary_contact_name,
                    primary_contact_email,
                    primary_contact_phone,
                    created_at,
                    updated_at
                FROM institution.institution
                WHERE id = :id
            """),
            {"id": institution_id},
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Institution not found.")

    if row.suspended_at:
        raise HTTPException(
            status_code=403,
            detail=row.suspension_reason or "This institution has been suspended.",
        )

    r = row._mapping

    return {
        # Gate fields (used by detectUserType)
        "user_type":         "institution",

        # Full profile (used by useMyInstitution / settings page)
        "id":                    str(r["id"]),
        "institution_id":        str(r["id"]),
        "name":                  r["name"],
        "legal_name":            r["legal_name"],
        "institution_type":      r["institution_type"],
        "reg_number":            r["reg_number"],
        "country":               r["country"],
        "regulator":             r["regulator"],
        "website":               r["website"],
        "deployment_model":      r["deployment_model"],
        "modules":               r["modules"] if isinstance(r["modules"], list) else [],
        "onboarding_stage":      r["onboarding_stage"],
        "compliance_status":     r["compliance_status"],
        "compliance_notes":      r["compliance_notes"],
        "approved":              r["approved"],
        "approved_at":           r["approved_at"].isoformat() if r["approved_at"] else None,
        "suspended_at":          r["suspended_at"].isoformat() if r["suspended_at"] else None,
        "suspension_reason":     r["suspension_reason"],
        "primary_contact_name":  r["primary_contact_name"],
        "primary_contact_email": r["primary_contact_email"],
        "primary_contact_phone": r["primary_contact_phone"],
        "created_at":            r["created_at"].isoformat(),
        "updated_at":            r["updated_at"].isoformat(),
    }
