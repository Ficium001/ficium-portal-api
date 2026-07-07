# =============================================================================
# ficium-portal-api — Approvals router (maker-checker core)
# Replaces: usePendingActions, useSubmitBid, useApproveAction, useRejectAction
#
# These call the SAME SECURITY DEFINER RPCs the frontend used on Supabase:
#   submit_for_approval(p_action_category, p_resource_type, p_resource_id, p_payload)
#   approve_action(p_action_id, p_note)
#   reject_action(p_action_id, p_note)
# Dual-control enforcement lives in those functions — unchanged.
# =============================================================================

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..deps import current_claims as get_claims
from ..deps import tenant_conn

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _row_to_dict(row) -> dict:
    return dict(row._mapping)


# ── Category → module permission mapping ──────────────────────────────────────
# Determines which module_permissions a caller needs to see/approve an action.
# The prefix is split_part(category, '.', 1).
CATEGORY_MODULE: dict[str, str] = {
    "bid":      "inst:bid_approval",
    "benefit":  "inst:benefits",
    "product":  "inst:products",
    "user":     "inst:team",
    "group":    "inst:team",
    "api_key":  "inst:settings",
    "sla":      "inst:settings",
    "webhook":  "inst:webhooks",
    "document": "inst:documents",
}


def _caller_permitted(category: str, claims: dict) -> bool:
    """Return True if the caller's module_permissions cover this action category."""
    is_super = claims.get("user_role", "") in INSTITUTION_ADMIN_ROLES
    if is_super:
        return True
    prefix  = category.split(".")[0]
    module  = CATEGORY_MODULE.get(prefix)
    perms   = claims.get("module_permissions", [])
    return module is None or module in perms


def _check_action_permission(
    action_id: str, claims: dict, conn: Session
) -> None:
    """
    Fetch the action category and verify the caller has the required module.
    Raises 404 if not found (scoped by institution via RLS), 403 if not permitted.
    """
    row = conn.execute(
        text("SELECT category FROM governance.action WHERE id = :aid LIMIT 1"),
        {"aid": action_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found.")
    if not _caller_permitted(row.category, claims):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have permission to action '{row.category}' requests.",
        )


@router.get("/pending")
async def list_pending_actions(
    conn:   Session = Depends(tenant_conn),
    claims: dict    = Depends(get_claims),
) -> list[dict]:
    """
    Pending maker-checker actions scoped to the caller's module permissions.
    Only actions whose category maps to a module the caller holds are returned.
    e.g. bid.* requires inst:bid_approval; benefit.* requires inst:products.
    """
    # Map action-category prefix → required module permission
    CATEGORY_MODULE: dict[str, str] = {
        "bid":         "inst:bid_approval",
        "benefit":     "inst:benefits",
        "user":        "inst:team",
        "group":       "inst:team",
        "api_key":     "inst:settings",
        "webhook":     "inst:webhooks",
        "document":    "inst:documents",
        "product":     "inst:products",
        "sla":         "inst:settings",
    }

    perms: list[str] = claims.get("module_permissions", [])
    # super_admin bypass — if role is super_admin see everything
    is_super = claims.get("user_role", "") in INSTITUTION_ADMIN_ROLES

    # Build the set of permitted category prefixes
    allowed_prefixes: list[str] = []
    for prefix, module in CATEGORY_MODULE.items():
        if is_super or module in perms:
            allowed_prefixes.append(prefix)

    if not allowed_prefixes and not is_super:
        return []

    # Filter by split_part(category, '.', 1) IN (allowed_prefixes)
    rows = conn.execute(
        text("""
            SELECT
                id,
                category          AS action_category,
                status            AS action_status,
                scope,
                label,
                risk,
                institution_id,
                maker_id,
                maker_role,
                resource_type,
                resource_id,
                resource_label,
                payload,
                payload_before,
                checker_id,
                checker_role,
                checker_note,
                checked_at,
                execution_status,
                executed_at,
                execution_error,
                expires_at,
                created_at        AS initiated_at,
                created_at,
                updated_at
            FROM governance.action
            WHERE status = 'pending'
              AND (
                :is_super
                OR split_part(category, '.', 1) = ANY(:prefixes)
              )
            ORDER BY expires_at ASC NULLS LAST
        """),
        {
            "is_super": is_super,
            "prefixes": allowed_prefixes,
        }
    ).fetchall()
    result = []
    for row in rows:
        r = _row_to_dict(row)
        for k in ("id", "institution_id", "maker_id", "checker_id", "resource_id"):
            if r.get(k) is not None:
                r[k] = str(r[k])
        for k in ("checked_at", "executed_at", "expires_at", "initiated_at", "created_at", "updated_at"):
            if r.get(k) is not None:
                r[k] = r[k].isoformat()
        result.append(r)
    return result


@router.post("/submit")
async def submit_for_approval(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Submit an action for dual-control approval.
    body: { action_category, resource_type, resource_id?, payload? }

    Blocks submission if a pending action already exists for the same
    resource_id + resource type within the institution (resource lock).
    """
    missing = {"action_category", "resource_type"} - set(body)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required fields: {sorted(missing)}",
        )

    resource_id  = body.get("resource_id")
    action_category = body["action_category"]

    # ── Resource lock: block if another pending action exists for this resource ──
    if resource_id:
        # Extract the domain prefix (e.g. "group" from "group.update_modules")
        domain = action_category.split(".")[0]
        conflict = conn.execute(
            text("""
                SELECT id, category
                FROM governance.action
                WHERE resource_id   = :rid
                  AND status        = 'pending'
                  AND split_part(category, '.', 1) = :domain
                LIMIT 1
            """),
            {"rid": resource_id, "domain": domain},
        ).fetchone()
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A pending '{conflict.category}' action already exists for this resource. "
                    f"It must be approved or rejected before new changes can be submitted."
                ),
            )

    result = conn.execute(
        text("""
            SELECT institution.submit_for_approval(
                :cat, :rtype, :rid, CAST(:payload AS jsonb)
            ) AS action_id
        """),
        {
            "cat":     action_category,
            "rtype":   body["resource_type"],
            "rid":     resource_id,
            "payload": json.dumps(body.get("payload", {})),
        },
    ).fetchone()
    if result is None:
        raise HTTPException(
            status_code=500,
            detail="submit_for_approval returned no action id.",
        )
    return {"action_id": str(result.action_id)}


@router.post("/{action_id}/approve")
async def approve_action(
    action_id: str,
    body:   dict    = Body(default={}),
    conn:   Session = Depends(tenant_conn),
    claims: dict    = Depends(get_claims),
) -> dict:
    """Approve a pending action — caller must hold the module for this action category."""
    _check_action_permission(action_id, claims, conn)
    try:
        result = conn.execute(
            text("SELECT institution.approve_action(:aid, :note) AS res"),
            {"aid": action_id, "note": body.get("note")},
        ).fetchone()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"result": result.res if result else None}


@router.post("/{action_id}/reject")
async def reject_action(
    action_id: str,
    body:   dict    = Body(...),
    conn:   Session = Depends(tenant_conn),
    claims: dict    = Depends(get_claims),
) -> dict:
    """Reject a pending action — caller must hold the module for this action category."""
    note = body.get("note")
    if not note:
        raise HTTPException(status_code=422, detail="A rejection note is required.")
    _check_action_permission(action_id, claims, conn)
    try:
        result = conn.execute(
            text("SELECT institution.reject_action(:aid, :note) AS res"),
            {"aid": action_id, "note": note},
        ).fetchone()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"result": result.res if result else None}


@router.post("/{action_id}/execute-update")
async def execute_user_update(
    action_id: str,
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Execute a user.update action after checker approval.
    Applies field changes (full_name, email, member_role) to the member.
    """
    row = conn.execute(
        text("SELECT id, category, status, payload FROM governance.action WHERE id = :aid"),
        {"aid": action_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found.")
    if row.category != "user.update":
        raise HTTPException(status_code=400, detail="Not a user.update action.")
    if row.status != "approved":
        raise HTTPException(status_code=400, detail=f"Action not approved (status: {row.status}).")

    payload     = row.payload
    member_id   = payload.get("member_id")
    field       = payload.get("field")
    value       = payload.get("value")

    if not member_id or not field or value is None:
        raise HTTPException(status_code=400, detail="Invalid payload — missing member_id, field or value.")

    allowed_fields = {"full_name", "email", "member_role"}
    if field not in allowed_fields:
        raise HTTPException(status_code=400, detail=f"Invalid field '{field}'.")

    # Apply to institution.member
    conn.execute(
        text(f"UPDATE institution.member SET {field} = :value, updated_at = now() WHERE id = :mid"),
        {"value": value, "mid": member_id},
    )

    # If email, also update auth_portal.auth_users
    if field == "email":
        member = conn.execute(
            text("SELECT auth_user_id FROM institution.member WHERE id = :mid"),
            {"mid": member_id},
        ).fetchone()
        if member and member.auth_user_id:
            conn.execute(
                text("UPDATE auth_portal.auth_users SET email = :value, updated_at = now() WHERE id = :uid"),
                {"value": value, "uid": str(member.auth_user_id)},
            )

    conn.execute(
        text("UPDATE governance.action SET execution_status = 'executed', executed_at = now() WHERE id = :aid"),
        {"aid": action_id},
    )
    conn.commit()
    return {"ok": True, "field": field, "value": value}


@router.post("/{action_id}/provision-user")
async def provision_user_from_action(
    action_id: str,
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Provision a new institution user after a user.create action is approved.

    Creates:
      - auth_portal.auth_users  (ficium-auth login entry, temp password)
      - institution.member      (links user to institution + group)

    Returns the temporary password so the admin can share it with the new user.
    Email-based invite can be added later via SMTP integration.
    """
    import secrets
    import string

    from argon2 import PasswordHasher

    # 1. Load the approved action (scoped to caller's institution via tenant_conn)
    row = conn.execute(
        text("""
            SELECT a.id, a.category, a.status, a.institution_id, a.payload
            FROM governance.action a
            WHERE a.id = :aid
        """),
        {"aid": action_id},
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Action not found.")
    if row.category != "user.create":
        raise HTTPException(status_code=400, detail="Action is not a user.create action.")
    if row.status != "approved":
        raise HTTPException(status_code=400, detail=f"Action is not approved (status: {row.status}).")

    payload        = row.payload
    email          = (payload.get("email") or "").strip().lower()
    username       = (payload.get("username") or "").strip().lower()
    first_name     = (payload.get("first_name") or "").strip()
    last_name      = (payload.get("last_name") or "").strip()
    full_name      = f"{first_name} {last_name}".strip() or email
    custom_group_id = payload.get("custom_group_id")
    member_role    = payload.get("member_role", "maker")
    institution_id = str(row.institution_id)

    if not email:
        raise HTTPException(status_code=400, detail="No email in action payload.")
    if not username:
        raise HTTPException(status_code=400, detail="No username in action payload.")
    if not custom_group_id:
        raise HTTPException(status_code=400, detail="No custom_group_id in action payload.")

    # 2. Idempotency — if auth_portal entry already exists, return early
    existing = conn.execute(
        text("SELECT id FROM auth_portal.auth_users WHERE email = :email LIMIT 1"),
        {"email": email},
    ).fetchone()
    if existing:
        # Also ensure member row exists
        member_exists = conn.execute(
            text("SELECT id FROM institution.member WHERE auth_user_id = :uid AND institution_id = :iid LIMIT 1"),
            {"uid": str(existing.id), "iid": institution_id},
        ).fetchone()
        if not member_exists:
            conn.execute(
                text("""
                    INSERT INTO institution.member
                        (institution_id, auth_user_id, email, full_name, role,
                         is_primary_admin, active, custom_group_id, member_role,
                         group_id, system_group_id)
                    VALUES (:iid, :uid, :email, :full_name, 'member',
                            false, true, :cgid, :mrole,
                            (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1),
                            (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1))
                """),
                {"iid": institution_id, "uid": str(existing.id), "email": email,
                 "full_name": full_name, "cgid": custom_group_id, "mrole": member_role},
            )
            conn.commit()
        return {"ok": True, "created": False, "message": "User already provisioned."}

    # 3. Generate a secure temp password
    alphabet    = string.ascii_letters + string.digits + "!@#$%"
    temp_password = "".join(secrets.choice(alphabet) for _ in range(16))

    hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4, hash_len=32, salt_len=16)
    pw_hash = hasher.hash(temp_password)

    # 4. Create auth_portal.auth_users
    new_user = conn.execute(
        text("""
            INSERT INTO auth_portal.auth_users
                (institution_id, email, username, role, password_hash, is_active,
                 email_verified, created_at, updated_at)
            VALUES (:iid, :email, :username, 'institution_member', :pw, true,
                    true, now(), now())
            RETURNING id
        """),
        {"iid": institution_id, "email": email, "username": username, "pw": pw_hash},
    ).fetchone()

    if new_user is None:
        raise HTTPException(status_code=500, detail="Auth user record could not be created.")
    new_user_id = str(new_user.id)

    # 5. Create institution.member
    conn.execute(
        text("""
            INSERT INTO institution.member
                (institution_id, auth_user_id, email, full_name, role,
                 is_primary_admin, active, custom_group_id, member_role,
                 group_id, system_group_id)
            VALUES (:iid, :uid, :email, :full_name, 'member',
                    false, true, :cgid, :mrole,
                    (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1),
                    (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1))
        """),
        {"iid": institution_id, "uid": new_user_id, "email": email,
         "full_name": full_name, "cgid": custom_group_id, "mrole": member_role},
    )

    # 6. Mark execution_status as completed on the governance action
    conn.execute(
        text("""
            UPDATE governance.action
            SET execution_status = 'executed', executed_at = now()
            WHERE id = :aid
        """),
        {"aid": action_id},
    )

    conn.commit()

    return {
        "ok": True,
        "created": True,
        "user_id": new_user_id,
        "email": email,
        "username": username,
        "full_name": full_name,
        "temp_password": temp_password,
        "message": "User provisioned. Share the temporary password with the user — they must change it on first login.",
    }
