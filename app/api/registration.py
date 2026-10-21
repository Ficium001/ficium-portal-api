# =============================================================================
# ficium-portal-api — Institution self-registration (one identity system)
#
#   POST /auth/register
#
# Replaces the browser flow that signed up through Supabase Auth, inserted the
# institution and first member through Supabase's public API, then called
# /auth/provision-member. That flow is why open Supabase sign-up and public
# access to the institution schema had to stay on (the 2026-10-19 takeover).
#
# Now ONE server-side transaction creates, or creates nothing:
#   * institution.institution   unapproved, 'registered', marketplace only
#   * auth_portal.auth_users    the ficium-auth login (Argon2id)
#   * institution.member        primary admin, institution_admin group
# No Supabase Auth user is created; ids come from ficium-auth (db/047 moved the
# last references to auth.users). Ficium staff approve the institution before
# it can do anything (onboarding stages, compliance review).
# =============================================================================
from __future__ import annotations

import re
import uuid
from typing import Any

from argon2 import PasswordHasher
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from ..core.db import service_session
from ..core.ratelimit import limiter

router = APIRouter(prefix="/auth", tags=["registration"])

_hasher = PasswordHasher()
# Must match the portal's registration form (RegisterInstitution.tsx).
_INSTITUTION_TYPES = {"commercial_bank", "fintech", "micro_credit", "leasing",
                      "insurance", "cooperative", "other"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def password_problem(pw: str) -> str | None:
    """Bank-grade minimum: 12+ characters and 3 of 4 character classes."""
    if len(pw) < 12:
        return "Password must be at least 12 characters."
    classes = sum(bool(re.search(p, pw)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w\s]"))
    if classes < 3:
        return "Use at least three of: lower case, upper case, digits, symbols."
    return None


class RegisterBody(BaseModel):
    institution_name: str = Field(min_length=2, max_length=200)
    institution_type: str = Field(default="commercial_bank")
    country:          str = Field(default="MU", min_length=2, max_length=2)
    reg_number:       str | None = Field(default=None, max_length=100)
    regulator:        str | None = Field(default=None, max_length=100)
    website:          str | None = Field(default=None, max_length=300)
    first_name:       str = Field(min_length=1, max_length=100)
    last_name:        str = Field(min_length=1, max_length=100)
    email:            str = Field(max_length=254)
    phone:            str | None = Field(default=None, max_length=40)
    password:         str = Field(min_length=1, max_length=200)

    @field_validator("institution_type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in _INSTITUTION_TYPES:
            raise ValueError(f"institution_type must be one of {sorted(_INSTITUTION_TYPES)}")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        return v.upper()


@router.post("/register", status_code=201)
@limiter.limit("5/hour")
async def register(request: Request, response: Response, body: RegisterBody) -> dict[str, Any]:
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    email = body.email
    full_name = f"{body.first_name.strip()} {body.last_name.strip()}"
    login_id = str(uuid.uuid4())
    pw_hash = _hasher.hash(body.password)

    with service_session() as conn:
        taken = conn.execute(
            text("""
                SELECT 1 FROM auth_portal.auth_users WHERE lower(email) = :e
                UNION ALL
                SELECT 1 FROM institution.member WHERE lower(email) = :e
                LIMIT 1
            """),
            {"e": email},
        ).fetchone()
        if taken:
            # Same answer whatever the reason: no account enumeration.
            raise HTTPException(status_code=409, detail="Registration could not be completed "
                                "with these details. If you already have an account, sign in.")

        inst = conn.execute(
            text("""
                INSERT INTO institution.institution
                  (name, legal_name, institution_type, country, reg_number, regulator,
                   website, deployment_model, modules, onboarding_stage, compliance_status,
                   approved, primary_contact_email, primary_contact_name, primary_contact_phone)
                VALUES
                  (:name, :name, :type, :country, :reg, :regulator,
                   :website, 'saas', '["marketplace"]'::jsonb, 'registered', 'not_submitted',
                   false, :email, :full_name, :phone)
                RETURNING id
            """),
            {"name": body.institution_name.strip(), "type": body.institution_type,
             "country": body.country, "reg": body.reg_number, "regulator": body.regulator,
             "website": body.website, "email": email, "full_name": full_name,
             "phone": body.phone},
        ).fetchone()
        if inst is None:
            raise HTTPException(status_code=500, detail="Registration failed.")

        conn.execute(
            text("""
                INSERT INTO auth_portal.auth_users
                    (id, email, role, institution_id, password_hash, is_active,
                     created_at, updated_at)
                VALUES (:id, :email, 'institution_admin', :iid, :pw, true, now(), now())
            """),
            {"id": login_id, "email": email, "iid": inst.id, "pw": pw_hash},
        )
        conn.execute(
            text("""
                INSERT INTO institution.member
                  (institution_id, auth_user_id, email, full_name, role,
                   is_primary_admin, active, group_id)
                VALUES (:iid, :uid, :email, :full_name, 'admin', true, true,
                        (SELECT id FROM portal_admin.user_groups
                          WHERE slug = 'institution_admin' LIMIT 1))
            """),
            {"iid": inst.id, "uid": login_id, "email": email, "full_name": full_name},
        )
        conn.execute(
            text("""
                INSERT INTO audit.event (actor_id, actor_type, institution_id, action,
                                         resource_type, resource_id, outcome)
                VALUES (CAST(:uid AS uuid), 'member', :iid, 'institution.registered',
                        'institution', :iid, 'success')
            """),
            {"uid": login_id, "iid": inst.id},
        )

    return {
        "status": "registered",
        "institution_id": str(inst.id),
        "email": email,
        "message": "Registered. Sign in with your email and password to continue onboarding.",
    }
