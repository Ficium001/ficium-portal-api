# =============================================================================
# ficium-portal-api — Institution onboarding submit
#
#   GET  /onboarding/requirements   documents this institution must provide
#   POST /onboarding/submit         save preferences + move to commercial review
#
# Replaces the browser flow that (a) uploaded straight to storage without
# creating institution.doc rows, (b) upserted a table that never existed and
# (c) called a function that never existed, ignoring both errors: every
# onboarding silently lost its data and stayed 'registered' (found 2026-10-21).
# Documents now go through /documents/upload-url + POST /documents (recorded,
# reviewable). Which documents are required comes from ONE database rule,
# institution.required_doc_types() (db/049), shared with the compliance view.
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..core.db import service_session
from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..deps import current_claims

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingPrefs(BaseModel):
    deployment_model: str = Field(pattern=r"^(saas|paas|on_prem)$")
    integration_mode: str = Field(min_length=1, max_length=50)
    it_contact_name:  str = Field(min_length=1, max_length=200)
    it_contact_email: str = Field(min_length=3, max_length=254,
                                  pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    webhook_url:      str | None = Field(default=None, max_length=500, pattern=r"^https://")
    expected_volume:  str | None = Field(default=None, max_length=50)


def _caller(claims: dict[str, Any]) -> tuple[str, str]:
    iid, sub = claims.get("institution_id"), claims.get("sub")
    if not iid or not sub:
        raise HTTPException(status_code=403, detail="No institution context.")
    if claims.get("user_role") not in INSTITUTION_ADMIN_ROLES:
        raise HTTPException(status_code=403,
                            detail="Only your institution's admin can submit onboarding.")
    return str(iid), str(sub)


def _requirements(conn: Any, iid: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("""
            SELECT dt.id, dt.code, dt.label, dt.description,
                   EXISTS (SELECT 1 FROM institution.doc d
                            WHERE d.institution_id = :iid AND d.doc_type_id = dt.id
                              AND d.status <> 'rejected') AS uploaded
              FROM institution.required_doc_types(CAST(:iid AS uuid)) dt
        """),
        {"iid": iid},
    ).fetchall()
    return [{"doc_type_id": str(r.id), "code": r.code, "label": r.label,
             "description": r.description, "uploaded": bool(r.uploaded)} for r in rows]


@router.get("/requirements")
async def requirements(claims: dict = Depends(current_claims)) -> list[dict[str, Any]]:
    iid, _ = _caller(claims)
    with service_session() as conn:
        return _requirements(conn, iid)


@router.post("/submit")
async def submit(body: OnboardingPrefs, claims: dict = Depends(current_claims)) -> dict[str, Any]:
    iid, sub = _caller(claims)
    with service_session() as conn:
        inst = conn.execute(
            text("SELECT onboarding_stage FROM institution.institution WHERE id = :iid FOR UPDATE"),
            {"iid": iid},
        ).fetchone()
        if inst is None:
            raise HTTPException(status_code=404, detail="Institution not found.")
        if inst.onboarding_stage != "registered":
            raise HTTPException(status_code=409,
                                detail="Onboarding has already been submitted for review.")
        member = conn.execute(
            text("""SELECT 1 FROM institution.member
                     WHERE institution_id = :iid AND auth_user_id = :sub AND active"""),
            {"iid": iid, "sub": sub},
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=403, detail="Not an active member of this institution.")

        missing = [r["label"] for r in _requirements(conn, iid) if not r["uploaded"]]
        if missing:
            raise HTTPException(status_code=422,
                                detail="Upload these documents first: " + ", ".join(missing) + ".")

        conn.execute(
            text("""
                INSERT INTO institution.onboarding_pref
                  (institution_id, deployment_model, integration_mode, it_contact_name,
                   it_contact_email, webhook_url, expected_volume, submitted_by)
                VALUES (:iid, :dm, :im, :itn, :ite, :wh, :vol, :sub)
                ON CONFLICT (institution_id) DO UPDATE SET
                  deployment_model = excluded.deployment_model,
                  integration_mode = excluded.integration_mode,
                  it_contact_name  = excluded.it_contact_name,
                  it_contact_email = excluded.it_contact_email,
                  webhook_url      = excluded.webhook_url,
                  expected_volume  = excluded.expected_volume,
                  submitted_by     = excluded.submitted_by,
                  submitted_at     = now()
            """),
            {"iid": iid, "dm": body.deployment_model, "im": body.integration_mode,
             "itn": body.it_contact_name, "ite": body.it_contact_email.lower(),
             "wh": body.webhook_url, "vol": body.expected_volume, "sub": sub},
        )
        conn.execute(
            text("""UPDATE institution.institution
                       SET onboarding_stage = 'commercial_review', updated_at = now()
                     WHERE id = :iid"""),
            {"iid": iid},
        )
        conn.execute(
            text("""
                INSERT INTO audit.event (actor_id, actor_type, institution_id, action,
                                         resource_type, resource_id, outcome)
                VALUES (CAST(:sub AS uuid), 'member', :iid, 'institution.onboarding_submitted',
                        'institution', :iid, 'success')
            """),
            {"sub": sub, "iid": iid},
        )
    return {"status": "commercial_review",
            "message": "Submitted. Ficium will review your documents and contact you."}
