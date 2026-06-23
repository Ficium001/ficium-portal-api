# =============================================================================
# ficium-portal-api — Members router
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/members", tags=["members"])


def _row(row) -> dict:
    return dict(row._mapping)


@router.get("/me")
async def get_my_role(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """The caller's own institution_members row."""
    sub = claims.get("sub")
    row = conn.execute(
        text("""
            SELECT *
            FROM institution.member
            WHERE auth_user_id = :uid
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")
    return _row(row)


@router.get("/my-group")
async def get_my_group(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict | None:
    """
    Resolve the caller's group and module_permissions.
    Uses sub from the verified JWT directly (avoids auth.uid() inside
    SECURITY DEFINER which may return null in pgbouncer transaction mode).
    Priority: custom institution group > platform system group.
    """
    sub = claims.get("sub")
    if not sub:
        return None

    # 0. Admin path — platform admins live in admin.user, not institution.member.
    #    Their modules come from admin.system_group (super_admin → ["*"]).
    if claims.get("user_role") in ("admin", "super_admin"):
        row = conn.execute(
            text("""
                SELECT jsonb_build_object(
                    'id',                 sg.id::TEXT,
                    'slug',               sg.slug,
                    'label',              sg.label,
                    'description',        COALESCE(sg.description, ''),
                    'module_permissions', to_jsonb(sg.module_permissions),
                    'user_type',          'admin',
                    'is_system',          true
                ) AS grp
                FROM  admin."user" u
                JOIN  admin.system_group sg ON sg.id = u.system_group_id
                WHERE u.auth_user_id = :uid
                LIMIT 1
            """),
            {"uid": sub},
        ).fetchone()
        if row and row.grp:
            return row.grp
        return None

    # 1. Custom institution group
    row = conn.execute(
        text("""
            SELECT jsonb_build_object(
                'id',                 g.id::TEXT,
                'slug',               g.slug,
                'label',              g.label,
                'description',        COALESCE(g.description, ''),
                'module_permissions', to_jsonb(g.module_permissions),
                'user_type',          'institution',
                'is_system',          g.is_system
            ) AS grp
            FROM  institution.member im
            JOIN  institution.group g ON g.id = im.custom_group_id
            WHERE im.auth_user_id = :uid
              AND im.active       = true
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row and row.grp:
        return row.grp

    # 2. Platform system group fallback (group_id -> portal_admin.user_groups)
    row = conn.execute(
        text("""
            SELECT jsonb_build_object(
                'id',                 ug.id::TEXT,
                'slug',               ug.slug,
                'label',              ug.label,
                'description',        COALESCE(ug.description, ''),
                'module_permissions', to_jsonb(ug.module_permissions),
                'user_type',          'institution',
                'is_system',          ug.is_system
            ) AS grp
            FROM  institution.member im
            JOIN  portal_admin.user_groups ug ON ug.id = im.group_id
            WHERE im.auth_user_id = :uid
              AND im.active       = true
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    if row and row.grp:
        return row.grp

    return None


@router.get("/my-group-debug")
async def get_my_group_debug(
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Temporary: returns raw member row to confirm sub + active state."""
    sub = claims.get("sub")
    row = conn.execute(
        text("""
            SELECT id, auth_user_id, active, group_id, custom_group_id, is_primary_admin
            FROM institution.member
            WHERE auth_user_id = :uid
            LIMIT 1
        """),
        {"uid": sub},
    ).fetchone()
    return {"sub": sub, "member": dict(row._mapping) if row else None}


@router.get("")
async def list_members(
    include_inactive: bool = False,
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """All members of the caller's institution (RLS-scoped). Active only by default."""
    where = "" if include_inactive else "WHERE active = true"
    rows = conn.execute(
        text(f"""
            SELECT m.*, au.is_active AS auth_active
            FROM institution.member m
            LEFT JOIN auth_portal.auth_users au ON au.id = m.auth_user_id
            {where}
            ORDER BY m.created_at
        """)
    ).fetchall()
    return [_serialize(dict(r._mapping)) for r in rows]


@router.get("/pending")
async def list_pending_member_actions(
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """Pending user.create and user.assign_group actions for this institution."""
    rows = conn.execute(
        text("""
            SELECT
                id, action_category, action_status, maker_id, maker_role,
                institution_id, initiated_at, resource_type, resource_id,
                payload, payload_before, checker_id, checker_role,
                checker_note, checked_at, expires_at, executed_at,
                execution_error, created_at
            FROM institution.pending_actions
            WHERE action_status = 'pending'
              AND action_category IN ('user.create', 'user.assign_group')
            ORDER BY initiated_at DESC
        """)
    ).fetchall()
    result = []
    for r in rows:
        m = dict(r._mapping)
        for k in ("id", "maker_id", "institution_id", "checker_id", "resource_id"):
            if m.get(k) is not None:
                m[k] = str(m[k])
        for k in ("initiated_at", "checked_at", "expires_at", "executed_at", "created_at"):
            if m.get(k) is not None:
                m[k] = m[k].isoformat()
        result.append(m)
    return result


# =============================================================================
# Member management endpoints
# =============================================================================

from fastapi import Body
import secrets
import string
from argon2 import PasswordHasher


def _hasher() -> PasswordHasher:
    return PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4, hash_len=32, salt_len=16)


def _serialize(m: dict) -> dict:
    for k in ("id", "auth_user_id", "institution_id", "group_id", "system_group_id", "custom_group_id"):
        if m.get(k) is not None:
            m[k] = str(m[k])
    for k in ("created_at", "updated_at"):
        if m.get(k) is not None:
            m[k] = m[k].isoformat()
    return m


@router.get("/{member_id}")
async def get_member(
    member_id: str,
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Get a single member by ID (scoped to caller's institution)."""
    row = conn.execute(
        text("""
            SELECT m.*, au.is_active AS auth_active
            FROM institution.member m
            LEFT JOIN auth_portal.auth_users au ON au.id = m.auth_user_id
            WHERE m.id = :mid
        """),
        {"mid": member_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")
    return _serialize(dict(row._mapping))


@router.patch("/{member_id}")
async def update_member(
    member_id: str,
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Update a member's full_name and/or member_role.
    Only institution admins can do this.
    Primary admin cannot be modified.
    """
    # Load member — ensure it exists in caller's institution (RLS via tenant_conn)
    row = conn.execute(
        text("SELECT id, is_primary_admin, auth_user_id FROM institution.member WHERE id = :mid"),
        {"mid": member_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")
    if row.is_primary_admin:
        raise HTTPException(status_code=403, detail="Primary admin cannot be modified.")

    updates = {}
    if "full_name" in body and body["full_name"]:
        updates["full_name"] = body["full_name"].strip()
    if "member_role" in body and body["member_role"]:
        allowed_roles = {"maker", "checker", "viewer", "analyst"}
        if body["member_role"] not in allowed_roles:
            raise HTTPException(status_code=422, detail=f"Invalid role. Must be one of: {sorted(allowed_roles)}")
        updates["member_role"] = body["member_role"]

    if not updates:
        raise HTTPException(status_code=422, detail="Nothing to update. Provide full_name or member_role.")

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["mid"] = member_id
    conn.execute(
        text(f"UPDATE institution.member SET {set_clause}, updated_at = now() WHERE id = :mid"),
        updates,
    )
    conn.commit()
    return {"ok": True, "updated": list(k for k in updates if k != "mid")}


@router.post("/{member_id}/deactivate")
async def deactivate_member(
    member_id: str,
    body: dict = Body(default={}),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Deactivate a member — removes portal access immediately.
    Sets active = false in institution.member and is_active = false in auth_portal.auth_users.
    Cannot deactivate primary admin.
    """
    row = conn.execute(
        text("SELECT id, is_primary_admin, auth_user_id FROM institution.member WHERE id = :mid"),
        {"mid": member_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")
    if row.is_primary_admin:
        raise HTTPException(status_code=403, detail="Primary admin cannot be deactivated.")

    conn.execute(
        text("UPDATE institution.member SET active = false, updated_at = now() WHERE id = :mid"),
        {"mid": member_id},
    )
    if row.auth_user_id:
        conn.execute(
            text("UPDATE auth_portal.auth_users SET is_active = false, updated_at = now() WHERE id = :uid"),
            {"uid": str(row.auth_user_id)},
        )
    conn.commit()
    return {"ok": True, "member_id": member_id, "status": "deactivated"}


@router.post("/{member_id}/reactivate")
async def reactivate_member(
    member_id: str,
    conn: Session = Depends(tenant_conn),
) -> dict:
    """Reactivate a previously deactivated member."""
    row = conn.execute(
        text("SELECT id, auth_user_id FROM institution.member WHERE id = :mid"),
        {"mid": member_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")

    conn.execute(
        text("UPDATE institution.member SET active = true, updated_at = now() WHERE id = :mid"),
        {"mid": member_id},
    )
    if row.auth_user_id:
        conn.execute(
            text("UPDATE auth_portal.auth_users SET is_active = true, updated_at = now() WHERE id = :uid"),
            {"uid": str(row.auth_user_id)},
        )
    conn.commit()
    return {"ok": True, "member_id": member_id, "status": "reactivated"}


@router.post("/{member_id}/reset-password")
async def reset_member_password(
    member_id: str,
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Generate a new temporary password for a member.
    Returns the temp password in plaintext — admin must share it with the user.
    """
    row = conn.execute(
        text("SELECT id, is_primary_admin, auth_user_id FROM institution.member WHERE id = :mid"),
        {"mid": member_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found.")
    if row.is_primary_admin:
        raise HTTPException(status_code=403, detail="Cannot reset primary admin password via this endpoint.")
    if not row.auth_user_id:
        raise HTTPException(status_code=400, detail="Member has no auth account yet.")

    alphabet = string.ascii_letters + string.digits + "!@#$%"
    temp_password = "".join(secrets.choice(alphabet) for _ in range(16))
    pw_hash = _hasher().hash(temp_password)

    conn.execute(
        text("UPDATE auth_portal.auth_users SET password_hash = :pw, updated_at = now() WHERE id = :uid"),
        {"pw": pw_hash, "uid": str(row.auth_user_id)},
    )
    conn.commit()
    return {
        "ok": True,
        "member_id": member_id,
        "temp_password": temp_password,
        "message": "Share this temporary password with the user. They should change it on next login.",
    }


@router.get("/{member_id}/audit")
async def get_member_audit(
    member_id: str,
    limit: int = 50,
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Per-user audit report:
      - Login history (identity.login_event)
      - Portal actions (audit.event)
      - Governance actions they initiated or checked (governance.action)
    """
    # Get member's auth_user_id and email
    member = conn.execute(
        text("SELECT auth_user_id, email, full_name, active FROM institution.member WHERE id = :mid"),
        {"mid": member_id},
    ).fetchone()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found.")

    auth_uid = str(member.auth_user_id) if member.auth_user_id else None

    # Login history
    logins = []
    if auth_uid:
        login_rows = conn.execute(
            text("""
                SELECT id, email, ip, user_agent, country, city, outcome, failure_reason, occurred_at
                FROM identity.login_event
                WHERE user_id = :uid
                ORDER BY occurred_at DESC
                LIMIT :lim
            """),
            {"uid": auth_uid, "lim": limit},
        ).fetchall()
        for r in login_rows:
            m = dict(r._mapping)
            m["id"] = str(m["id"]) if m.get("id") else None
            if m.get("occurred_at"):
                m["occurred_at"] = m["occurred_at"].isoformat()
            logins.append(m)

    # Portal actions (audit.event)
    actions = []
    if auth_uid:
        action_rows = conn.execute(
            text("""
                SELECT id, occurred_at, action, resource_type, resource_label,
                       outcome, outcome_note, actor_ip, session_id
                FROM audit.event
                WHERE actor_id = :uid
                ORDER BY occurred_at DESC
                LIMIT :lim
            """),
            {"uid": auth_uid, "lim": limit},
        ).fetchall()
        for r in action_rows:
            m = dict(r._mapping)
            m["id"] = str(m["id"]) if m.get("id") else None
            if m.get("occurred_at"):
                m["occurred_at"] = m["occurred_at"].isoformat()
            actions.append(m)

    # Governance actions they initiated
    gov_rows = conn.execute(
        text("""
            SELECT id, category, status, resource_type, resource_label,
                   maker_role, checker_role, created_at, checked_at
            FROM governance.action
            WHERE maker_id = :uid OR checker_id = :uid
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"uid": auth_uid, "lim": limit},
    ).fetchall() if auth_uid else []

    governance = []
    for r in gov_rows:
        m = dict(r._mapping)
        m["id"] = str(m["id"]) if m.get("id") else None
        for k in ("created_at", "checked_at"):
            if m.get(k):
                m[k] = m[k].isoformat()
        governance.append(m)

    return {
        "member": {
            "id": member_id,
            "email": member.email,
            "full_name": member.full_name,
            "active": member.active,
            "auth_user_id": auth_uid,
        },
        "logins": logins,
        "actions": actions,
        "governance": governance,
    }
