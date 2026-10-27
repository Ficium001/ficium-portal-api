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

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.db import service_session
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
    # Switching four-eyes off: only admins see and approve it (db/044).
    "four_eyes": "inst:settings",
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


# Visibility map for the queue screens: action-category prefix -> module key.
# (Deliberately not CATEGORY_MODULE above: that one also guards approve/reject
# and carries four_eyes; the list screens have always used this narrower set.)
_QUEUE_CATEGORY_MODULE: dict[str, str] = {
    "bid":      "inst:bid_approval",
    "benefit":  "inst:benefits",
    "user":     "inst:team",
    "group":    "inst:team",
    "api_key":  "inst:settings",
    "webhook":  "inst:webhooks",
    "document": "inst:documents",
    "product":  "inst:products",
    "sla":      "inst:settings",
}

# An approval is expired when it is still 'pending' past expires_at. The sweep
# (db/054, pg_cron every 5 min) writes that down, but reads never wait for it:
# this expression is the single definition of the state the screens show.
_EFFECTIVE_STATUS = (
    "CASE WHEN status = 'pending' AND expires_at IS NOT NULL AND expires_at <= now() "
    "THEN 'expired' ELSE status END"
)
_QUEUE_STATES = ("pending", "expired", "approved", "rejected")


def _queue_visibility(claims: dict, scope: str | None) -> tuple[bool, list[str]]:
    """Who sees what: (is_super, permitted category prefixes). Validates scope."""
    if scope is not None and scope not in ("bids", "internal"):
        raise HTTPException(status_code=422, detail="scope must be 'bids' or 'internal'.")
    perms: list[str] = claims.get("module_permissions", [])
    is_super = claims.get("user_role", "") in INSTITUTION_ADMIN_ROLES
    prefixes = [p for p, m in _QUEUE_CATEGORY_MODULE.items() if is_super or m in perms]
    return is_super, prefixes


def _maybe_my_member_id(conn: Session) -> str | None:
    """Caller's member id, or None. The queue is readable without it; it only
    powers the 'initiated by you' marker."""
    try:
        row = conn.execute(
            text("SELECT (institution.current_member_ctx()).member_id AS id")).fetchone()
    except Exception:
        return None
    return str(row.id) if row is not None and row.id is not None else None


_SCOPE_SQL = """
  AND (
    :is_super
    OR split_part(category, '.', 1) = ANY(:prefixes)
  )
  AND (
    :scope IS NULL
    OR (:scope = 'bids'     AND split_part(category, '.', 1) =  'bid')
    OR (:scope = 'internal' AND split_part(category, '.', 1) <> 'bid')
  )
"""

_ROW_COLUMNS = """
    id,
    category          AS action_category,
    {status}          AS action_status,
    scope, label, risk, institution_id, maker_id, maker_role,
    resource_type, resource_id, resource_label, payload, payload_before,
    checker_id, checker_role, checker_note, checked_at,
    execution_status, executed_at, execution_error, expires_at,
    created_at        AS initiated_at,
    created_at, updated_at
"""


def _serialise_action(row, me: str | None) -> dict:
    r = _row_to_dict(row)
    for k in ("id", "institution_id", "maker_id", "checker_id", "resource_id"):
        if r.get(k) is not None:
            r[k] = str(r[k])
    for k in ("checked_at", "executed_at", "expires_at", "initiated_at", "created_at", "updated_at"):
        if r.get(k) is not None:
            r[k] = r[k].isoformat()
    # Four-eyes: you can never approve your own. The UI uses this to keep your
    # own submissions out of "needs you" and to show them as waiting on others.
    r["is_mine"] = me is not None and r.get("maker_id") == me
    return r


@router.get("/pending")
async def list_pending_actions(
    scope:  str | None = None,
    conn:   Session = Depends(tenant_conn),
    claims: dict    = Depends(get_claims),
) -> list[dict]:
    """
    Pending maker-checker actions scoped to the caller's module permissions.
    Only actions whose category maps to a module the caller holds are returned.
    e.g. bid.* requires inst:bid_approval; benefit.* requires inst:products.

    `scope` optionally narrows to one queue:
      bids     — bid.* only        (Marketplace > Approvals screen)
      internal — everything but bid.* (Dual Control screen)

    The two portal screens partition this list between them. They used to each
    fetch the full set and discard the other's half client-side, which meant two
    over-fetching polls and a split rule duplicated in UI code where the halves
    could drift apart. Filtering here keeps the partition authoritative in one
    place and cuts each response to what its caller actually renders.
    """
    is_super, allowed_prefixes = _queue_visibility(claims, scope)
    if not allowed_prefixes and not is_super:
        return []

    rows = conn.execute(
        text(
            "SELECT " + _ROW_COLUMNS.format(status="status") + """
            FROM governance.action
            WHERE status = 'pending'
              AND (expires_at IS NULL OR expires_at > now())
            """ + _SCOPE_SQL + """
            ORDER BY expires_at ASC NULLS LAST
            """
        ),
        {"is_super": is_super, "prefixes": allowed_prefixes, "scope": scope},
    ).fetchall()
    me = _maybe_my_member_id(conn)
    return [_serialise_action(row, me) for row in rows]


@router.get("/queue")
async def list_queue(
    state:  str,
    scope:  str | None = None,
    limit:  int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn:   Session = Depends(tenant_conn),
    claims: dict    = Depends(get_claims),
) -> dict:
    """
    One lifecycle state of the approval queue: pending | expired | approved |
    rejected. Same visibility rules as /pending. `action_status` is the
    EFFECTIVE state, so a pending row past its expiry is reported as expired
    even if the sweep has not run yet.

    pending sorts soonest-expiry first (what to do next); the three finished
    states sort newest decision first. Paged with limit/offset; `total` is the
    full count for that state.
    """
    if state not in _QUEUE_STATES:
        raise HTTPException(
            status_code=422, detail=f"state must be one of {', '.join(_QUEUE_STATES)}.")
    is_super, prefixes = _queue_visibility(claims, scope)
    if not prefixes and not is_super:
        return {"items": [], "total": 0}

    params = {"is_super": is_super, "prefixes": prefixes, "scope": scope, "state": state}
    where = f"WHERE ({_EFFECTIVE_STATUS}) = :state " + _SCOPE_SQL
    total = conn.execute(
        text("SELECT count(*) AS n FROM governance.action " + where), params).scalar_one()
    order = ("expires_at ASC NULLS LAST" if state == "pending"
             else "COALESCE(checked_at, expires_at, updated_at) DESC, id")
    rows = conn.execute(
        text(
            "SELECT " + _ROW_COLUMNS.format(status=_EFFECTIVE_STATUS)
            + " FROM governance.action " + where
            + f" ORDER BY {order} LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": limit, "offset": offset},
    ).fetchall()
    me = _maybe_my_member_id(conn)
    return {"items": [_serialise_action(row, me) for row in rows], "total": total}


@router.get("/queue/counts")
async def queue_counts(
    scope:  str | None = None,
    conn:   Session = Depends(tenant_conn),
    claims: dict    = Depends(get_claims),
) -> dict[str, int]:
    """Counts for the four tabs, plus `needs_me`: pending items the caller did
    not initiate (the only ones they are allowed to decide)."""
    is_super, prefixes = _queue_visibility(claims, scope)
    zero = {s: 0 for s in _QUEUE_STATES} | {"needs_me": 0}
    if not prefixes and not is_super:
        return zero
    me = _maybe_my_member_id(conn)
    rows = conn.execute(
        text(
            f"SELECT ({_EFFECTIVE_STATUS}) AS state, count(*) AS n, "
            "count(*) FILTER (WHERE maker_id IS DISTINCT FROM CAST(:me AS uuid)) AS not_mine "
            "FROM governance.action WHERE true " + _SCOPE_SQL + " GROUP BY 1"
        ),
        {"is_super": is_super, "prefixes": prefixes, "scope": scope, "me": me},
    ).fetchall()
    out = dict(zero)
    for r in rows:
        if r.state in _QUEUE_STATES:
            out[r.state] = int(r.n)
        if r.state == "pending":
            out["needs_me"] = int(r.not_mine)
    return out


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
    # Four-eyes may be switched off for this action (Setup > Controls). Then
    # governance.submit has already executed it, as single control, audited.
    state = conn.execute(
        text("SELECT status, control FROM governance.action WHERE id = :aid"),
        {"aid": result.action_id},
    ).fetchone()
    executed = state is not None and state.control == "single" and state.status == "approved"
    return {
        "action_id": str(result.action_id),
        "status": "executed" if executed else "pending_approval",
    }


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
    Kept for older portal builds. Since migration 040 the database applies a
    user.update atomically when the checker approves (governance.execute), so
    this only reports the result. It used to apply the change itself in a
    second, browser-driven call, which could be skipped or replayed.
    """
    row = conn.execute(
        text("SELECT category, status, execution_status FROM governance.action WHERE id = :aid"),
        {"aid": action_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found.")
    if row.category != "user.update":
        raise HTTPException(status_code=400, detail="Not a user.update action.")
    if row.status != "approved" or row.execution_status != "executed":
        raise HTTPException(status_code=409, detail="Not executed: the action is not approved.")
    return {"ok": True}


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

    # 2. Everything below touches auth_portal, which tenant sessions can neither read nor write
    #    (RLS with no policies), so it runs in ONE privileged transaction. Authorisation is already
    #    established above: the action was loaded through the caller's tenant session, so it belongs
    #    to the caller's institution and is approved.
    alphabet    = string.ascii_letters + string.digits + "!@#$%"
    temp_password = "".join(secrets.choice(alphabet) for _ in range(16))
    hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4, hash_len=32, salt_len=16)
    pw_hash = hasher.hash(temp_password)

    insert_member = text("""
        INSERT INTO institution.member
            (institution_id, auth_user_id, email, full_name, role,
             is_primary_admin, active, custom_group_id, member_role,
             group_id)
        VALUES (:iid, :uid, :email, :full_name, 'member',
                false, true, :cgid, :mrole,
                (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1))
    """)
    member_params = {"iid": institution_id, "email": email, "full_name": full_name,
                     "cgid": custom_group_id, "mrole": member_role}

    me = _my_member_id(conn)
    try:
        with service_session() as svc:
            # Only the approving checker, once, in the same transaction as the
            # login it creates (db/043). Before: anyone, including the maker,
            # could call this first and receive the new user's password.
            _claim_credential(svc, action_id, "user.create", me)
            # Idempotency: an existing login for this email is reused ONLY inside this institution.
            existing = svc.execute(
                text("SELECT id, institution_id FROM auth_portal.auth_users WHERE email = :email LIMIT 1"),
                {"email": email},
            ).fetchone()
            if existing is not None:
                if str(existing.institution_id) != institution_id:
                    raise HTTPException(status_code=409, detail="That email is already registered to another institution.")
                member_exists = svc.execute(
                    text("SELECT id FROM institution.member WHERE auth_user_id = :uid AND institution_id = :iid LIMIT 1"),
                    {"uid": str(existing.id), "iid": institution_id},
                ).fetchone()
                if member_exists is None:
                    svc.execute(insert_member, {**member_params, "uid": str(existing.id)})
                return {"ok": True, "created": False, "message": "User already provisioned."}

            new_user = svc.execute(
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

            svc.execute(insert_member, {**member_params, "uid": new_user_id})
            svc.execute(
                text("UPDATE governance.action SET execution_status = 'executed', executed_at = now() WHERE id = :aid"),
                {"aid": action_id},
            )
    except IntegrityError:
        raise HTTPException(status_code=409, detail="That username or email is already in use.") from None

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


# ─── Credential release: once, only to the approving checker (db/043) ─────────

_CLAIM_CREDENTIAL = text("""
    UPDATE governance.action
       SET credential_released_at = now(),
           execution_status = 'executed', executed_at = now(), updated_at = now()
     WHERE id = :aid
       AND category = :cat
       AND status = 'approved'
       AND checker_id = :me
       AND credential_released_at IS NULL
    RETURNING payload, institution_id
""")


def _my_member_id(conn: Session) -> str:
    row = conn.execute(text("SELECT (institution.current_member_ctx()).member_id AS id")).fetchone()
    if row is None or row.id is None:
        raise HTTPException(status_code=403, detail="Not an active member of this institution.")
    return str(row.id)


def _claim_credential(svc: Session, action_id: str, category: str, me: str):
    """Claim the right to see a temporary password. Same transaction as the
    password write (service_session commits both or neither)."""
    claimed = svc.execute(_CLAIM_CREDENTIAL,
                          {"aid": action_id, "cat": category, "me": me}).fetchone()
    if claimed is None:
        raise HTTPException(
            status_code=409,
            detail="Only the person who approved this can see the password, and only once.",
        )
    return claimed


def _temp_password() -> str:
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(16))


@router.post("/{action_id}/release-password")
async def release_reset_password(
    action_id: str,
    claims: dict = Depends(get_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    After a user.reset_password action is approved, the CHECKER who approved
    it calls this once: a temporary password is set (user must change it at
    next login) and returned exactly once. Nobody else, and never twice.
    """
    from argon2 import PasswordHasher

    me = _my_member_id(conn)
    temp = _temp_password()
    with service_session() as svc:
        claimed = _claim_credential(svc, action_id, "user.reset_password", me)
        member = svc.execute(
            text("""
                SELECT m.auth_user_id, m.email, m.full_name
                  FROM institution.member m
                 WHERE m.id = CAST(:mid AS uuid) AND m.institution_id = :iid
                   AND m.auth_user_id IS NOT NULL AND NOT m.is_primary_admin
            """),
            {"mid": (claimed.payload or {}).get("member_id"), "iid": claimed.institution_id},
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=404, detail="That user can no longer be reset.")
        updated = svc.execute(
            text("""
                UPDATE auth_portal.auth_users
                   SET password_hash = :pw, must_change_password = true, updated_at = now()
                 WHERE id = :uid
                RETURNING id
            """),
            {"pw": PasswordHasher().hash(temp), "uid": str(member.auth_user_id)},
        ).fetchone()
        if updated is None:
            raise HTTPException(status_code=404, detail="Login record not found.")
        svc.execute(
            text("""
                INSERT INTO audit.event (actor_id, actor_type, institution_id, action,
                                         resource_type, outcome, governance_action_id)
                VALUES (CAST(:actor AS uuid), 'member', :iid, 'user.reset_password.released',
                        'member', 'success', CAST(:aid AS uuid))
            """),
            {"actor": claims.get("sub"), "iid": claimed.institution_id, "aid": action_id},
        )
    return {
        "ok": True,
        "email": member.email,
        "full_name": member.full_name,
        "temp_password": temp,
        "message": "Shown once. The user must change it at their next login.",
    }
