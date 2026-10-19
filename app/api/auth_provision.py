# =============================================================================
# ficium-portal-api — Auth provisioning router
#
# POST /auth/provision-member
#   Called by the portal frontend at the END of institution registration,
#   after Supabase auth.signUp() and institution/member row creation.
#   Creates the auth_portal.auth_users entry so the user can log in via
#   ficium-auth (which is what the portal login page uses).
#
# Security: no JWT required (user isn't logged in yet). It only ever creates
# the FIRST login of a brand-new institution, as institution_admin, and only
# when: the auth user exists; the caller is that institution's primary-admin
# member (insert gated by RLS, migration 038); the institution is unapproved,
# 'registered', and its contact email is the caller's; and no other login
# exists for it. Role is never read from the request.
# Until 2026-10-19 this accepted any institution id and any role.
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
    # Never taken from the request. Self-registration only ever creates the
    # primary admin of a brand-new, unapproved institution; every other
    # member is provisioned through dual control (approvals.py).
    role           = "institution_admin"

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

        # 2. The caller must be the primary admin of an institution THEY just
        #    registered: still unapproved, contact email = this user, and the
        #    member row (RLS-gated at insert, migration 038) is theirs.
        #    Previously any institution id + any role was accepted, which let
        #    anyone mint a login inside an approved bank (critical, 2026-10-19).
        inst_row = conn.execute(
            text("""
                SELECT i.id
                  FROM institution.institution i
                  JOIN institution.member m
                    ON m.institution_id = i.id
                   AND m.auth_user_id   = :uid
                   AND lower(m.email)   = :email
                   AND m.is_primary_admin
                 WHERE i.id = :iid
                   AND i.approved = false
                   AND i.onboarding_stage = 'registered'
                   AND lower(i.primary_contact_email) = :email
                 LIMIT 1
            """),
            {"iid": institution_id, "uid": auth_user_id, "email": email},
        ).fetchone()
        if inst_row is None:
            raise HTTPException(status_code=403, detail="Registration could not be verified.")

        # 2b. First login for this institution only — never a second one.
        taken = conn.execute(
            text("""
                SELECT 1 FROM auth_portal.auth_users
                 WHERE institution_id = :iid AND lower(email) <> :email
                 LIMIT 1
            """),
            {"iid": institution_id, "email": email},
        ).fetchone()
        if taken:
            raise HTTPException(status_code=403, detail="Registration could not be verified.")

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
