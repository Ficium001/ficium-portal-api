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
    Return the caller's full institution row.

    Admins are gated by role and have no institution row — return a minimal
    sentinel so the portal shell can redirect them correctly.
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
                FROM institution.institutions
                WHERE id = :id
            """),
            {"id": institution_id},
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Institution not found.")

    return {
        "user_type":             "institution",
        "id":                    str(row.id),
        "name":                  row.name,
        "legal_name":            row.legal_name,
        "institution_type":      row.institution_type,
        "reg_number":            row.reg_number,
        "country":               row.country,
        "regulator":             row.regulator,
        "website":               row.website,
        "deployment_model":      row.deployment_model,
        "modules":               row.modules or [],
        "onboarding_stage":      row.onboarding_stage,
        "compliance_status":     row.compliance_status,
        "compliance_notes":      row.compliance_notes,
        "approved":              row.approved,
        "approved_at":           row.approved_at.isoformat() if row.approved_at else None,
        "suspended_at":          row.suspended_at.isoformat() if row.suspended_at else None,
        "suspension_reason":     row.suspension_reason,
        "primary_contact_name":  row.primary_contact_name,
        "primary_contact_email": row.primary_contact_email,
        "primary_contact_phone": row.primary_contact_phone,
        "institution_id":        str(row.id),
        "created_at":            row.created_at.isoformat(),
        "updated_at":            row.updated_at.isoformat(),
    }
