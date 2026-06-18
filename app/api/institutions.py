# =============================================================================
# ficium-portal-api — Institutions router (v2 schema)
# Table: institution.institution (was institution.institutions)
# New fields: tax_id, incorporation_date, logo_url, timezone, notes, metadata
#             compliance_reviewed_at/by, approved_by, suspended_by, offboarded_at
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from ..deps import current_claims
from ..core.db import tenant_session

router = APIRouter(prefix="/institutions", tags=["institutions"])

_ADMIN_ROLES = ("admin", "super_admin", "ficium_admin", "platform_admin")


@router.get("/me")
async def get_my_institution(claims: dict = Depends(current_claims)) -> dict:
    """
    Return the caller's full institution row (v2 schema).
    Admins get a sentinel response — no institution row exists for them.
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

    with tenant_session(claims) as conn:
        row = conn.execute(
            text("""
                SELECT
                    id,
                    name,
                    legal_name,
                    institution_type,
                    reg_number,
                    tax_id,
                    incorporation_date,
                    country,
                    regulator,
                    website,
                    logo_url,
                    deployment_model,
                    timezone,
                    modules,
                    onboarding_stage,
                    compliance_status,
                    compliance_notes,
                    compliance_reviewed_at,
                    approved,
                    approved_at,
                    suspended_at,
                    suspension_reason,
                    offboarded_at,
                    primary_contact_name,
                    primary_contact_email,
                    primary_contact_phone,
                    notes,
                    metadata,
                    created_at,
                    updated_at
                FROM institution.institution
                WHERE id = :id
            """),
            {"id": institution_id},
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Institution not found.")

    return {
        "user_type":             "institution",
        "institution_id":        str(row.id),
        "id":                    str(row.id),
        "name":                  row.name,
        "legal_name":            row.legal_name,
        "institution_type":      row.institution_type,
        "reg_number":            row.reg_number,
        "tax_id":                row.tax_id,
        "incorporation_date":    row.incorporation_date.isoformat() if row.incorporation_date else None,
        "country":               row.country,
        "regulator":             row.regulator,
        "website":               row.website,
        "logo_url":              row.logo_url,
        "deployment_model":      row.deployment_model,
        "timezone":              row.timezone,
        "modules":               row.modules or [],
        "onboarding_stage":      row.onboarding_stage,
        "compliance_status":     row.compliance_status,
        "compliance_notes":      row.compliance_notes,
        "compliance_reviewed_at": row.compliance_reviewed_at.isoformat() if row.compliance_reviewed_at else None,
        "approved":              row.approved,
        "approved_at":           row.approved_at.isoformat() if row.approved_at else None,
        "suspended_at":          row.suspended_at.isoformat() if row.suspended_at else None,
        "suspension_reason":     row.suspension_reason,
        "offboarded_at":         row.offboarded_at.isoformat() if row.offboarded_at else None,
        "primary_contact_name":  row.primary_contact_name,
        "primary_contact_email": row.primary_contact_email,
        "primary_contact_phone": row.primary_contact_phone,
        "notes":                 row.notes,
        "metadata":              row.metadata or {},
        "created_at":            row.created_at.isoformat(),
        "updated_at":            row.updated_at.isoformat(),
    }
