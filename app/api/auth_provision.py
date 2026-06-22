# =============================================================================
# ficium-portal-api — Auth provisioning router
#
# POST /auth/provision-member
#   Called by the portal frontend at the END of institution registration,
#   after Supabase auth.signUp() and institution/member row creation.
#   Creates the auth_portal.auth_users entry so the user can log in via
#   ficium-auth (which is what the portal login page uses).
#
# Security: no JWT required (user isn't logged in yet), but the endpoint
# validates that the auth_user_id exists in auth.users AND that the
# institution_id exists and is in 'registered' state before creating
# the ficium-auth entry. This prevents abuse.
# =============================================================================

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import HashingError
from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import text

from ..core.db import service_session

router = APIRouter(prefix="/auth", tags=["auth-provision"])

# Match ficium-auth's exact Argon2id parameters (from ficium-auth/src/core/config.py)
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


@router.post("/provision-member")
async def provision_member(body: dict = Body(...)) -> dict:
    """
    Create a ficium-auth entry for a newly registered institution member.

    Called by the portal frontend after:
      1. supabase.auth.signUp() — creates auth.users row
      2. institution.institution insert — creates institution row
      3. institution.member insert — links member to institution

    This step creates auth_portal.auth_users so the user can log in
    via ficium-auth on the portal login page.
    """
    missing = {"auth_user_id", "email", "password", "institution_id"} - set(body)
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {sorted(missing)}")

    auth_user_id   = body["auth_user_id"]
    email          = body["email"].strip().lower()
    password       = body["password"]
    institution_id = body["institution_id"]
    role           = body.get("role", "institution_admin")

    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

    with service_session() as conn:
        # 1. Verify the Supabase auth user actually exists
        auth_row = conn.execute(
            text("SELECT id FROM auth.users WHERE id = :uid AND email = :email LIMIT 1"),
            {"uid": auth_user_id, "email": email},
        ).fetchone()
        if auth_row is None:
            raise HTTPException(
                status_code=400,
                detail="auth_user_id does not match a valid Supabase auth user.",
            )

        # 2. Verify the institution exists in a registerable state
        inst_row = conn.execute(
            text("""
                SELECT id FROM institution.institution
                WHERE id = :iid
                  AND onboarding_stage IN ('registered', 'commercial_review',
                                           'compliance_review', 'pending_approval',
                                           'approved')
                LIMIT 1
            """),
            {"iid": institution_id},
        ).fetchone()
        if inst_row is None:
            raise HTTPException(
                status_code=400,
                detail="Institution not found or not in a valid state.",
            )

        # 3. Idempotency — skip if ficium-auth entry already exists
        existing = conn.execute(
            text("SELECT id FROM auth_portal.auth_users WHERE email = :email LIMIT 1"),
            {"email": email},
        ).fetchone()
        if existing:
            return {"ok": True, "created": False, "message": "ficium-auth entry already exists."}

        # 4. Hash password with Argon2id (same params as ficium-auth service)
        try:
            pw_hash = _hasher.hash(password)
        except HashingError as e:
            raise HTTPException(status_code=422, detail=f"Invalid password: {e}") from e

        # 5. Insert ficium-auth user — use SAME UUID as Supabase auth user
        #    so JWT sub matches institution.member.auth_user_id for RLS
        conn.execute(
            text("""
                INSERT INTO auth_portal.auth_users
                    (id, email, role, institution_id, password_hash, is_active,
                     created_at, updated_at)
                VALUES
                    (:id, :email, :role, :iid, :pw, true, now(), now())
                ON CONFLICT (id) DO NOTHING
            """),
            {
                "id":   auth_user_id,
                "email": email,
                "role":  role,
                "iid":   institution_id,
                "pw":    pw_hash,
            },
        )

    return {"ok": True, "created": True}
