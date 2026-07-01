# =============================================================================
# ficium-portal-api — Institution API Key management router
#
# Endpoints (all under /api-keys, JWT-authenticated portal users only):
#
#   GET    /api-keys           list keys for the caller's institution
#   POST   /api-keys           request a new key (maker-checker)
#   PUT    /api-keys/{id}/approve    approve a pending key
#   POST   /api-keys/{id}/revoke     revoke an active key
#   DELETE /api-keys/{id}     delete a pending/revoked key
#
# Key is shown to the caller ONCE on creation in the response body.
# After that only key_prefix is exposed for identification.
# =============================================================================

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.api_keys import generate_api_key
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/api-keys", tags=["api-keys"])

_VALID_SCOPES = {
    "marketplace:read",
    "bids:write",
    "bids:read",
    "pipeline:write",
    "pipeline:read",
    "analytics:read",
    "documents:write",
    "webhooks:manage",
}


class CreateKeyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class ApproveKeyRequest(BaseModel):
    note: str | None = None


def _get_institution_id(claims: dict[str, Any]) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="No institution context.")
    return str(iid)


def _get_user_id(claims: dict[str, Any]) -> str:
    uid = claims.get("sub") or claims.get("user_id")
    if not uid:
        raise HTTPException(status_code=403, detail="Cannot determine user identity.")
    return str(uid)


@router.get("")
def list_keys(
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> list[dict]:
    iid = _get_institution_id(claims)
    rows = conn.execute(
        text("""
            SELECT
                ak.id,
                ak.label,
                ak.key_prefix,
                ak.scopes,
                ak.active,
                ak.mc_status,
                ak.created_at,
                ak.expires_at,
                ak.last_used_at,
                ak.last_used_ip::text,
                ak.revoked_at,
                ak.rejection_note,
                req.username AS requested_by_username,
                appr.username AS approved_by_username
            FROM institution.api_key ak
            LEFT JOIN admin."user" req  ON req.id  = ak.requested_by
            LEFT JOIN admin."user" appr ON appr.id = ak.approved_by
            WHERE ak.institution_id = CAST(:iid AS uuid)
              AND ak.revoked_at IS NULL
            ORDER BY ak.created_at DESC
        """),
        {"iid": iid},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("", status_code=201)
def create_key(
    body: Annotated[CreateKeyRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    """
    Request a new API key (requires maker-checker approval).
    Returns the raw key ONCE in this response — it cannot be retrieved again.
    """
    iid = _get_institution_id(claims)
    uid = _get_user_id(claims)

    # Validate scopes
    bad = [s for s in body.scopes if s not in _VALID_SCOPES]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown scopes: {bad}")

    raw_key, key_prefix, key_hash = generate_api_key()

    expires_sql = (
        "now() + CAST(:expires_days || ' days' AS interval)"
        if body.expires_days
        else "NULL"
    )

    row = conn.execute(
        text(f"""
            INSERT INTO institution.api_key
                (institution_id, label, key_prefix, key_hash, key_hmac,
                 scopes, active, mc_status, requested_by, expires_at)
            VALUES
                (CAST(:iid AS uuid), :label, :prefix, :hash, '',
                 CAST(:scopes AS text[]), false, 'pending_approval',
                 CAST(:uid AS uuid), {expires_sql})
            RETURNING id, label, key_prefix, scopes, mc_status, created_at, expires_at
        """),
        {
            "iid": iid,
            "label": body.label,
            "prefix": key_prefix,
            "hash": key_hash,
            "scopes": list(body.scopes),
            "uid": uid,
            "expires_days": body.expires_days,
        },
    ).fetchone()
    conn.commit()

    return {
        **dict(row._mapping),
        # Raw key shown ONCE — institution must store it securely
        "raw_key": raw_key,
        "warning": "This is the only time the full API key will be shown. Store it securely.",
    }


@router.put("/{key_id}/approve")
def approve_key(
    key_id: str,
    body: Annotated[ApproveKeyRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    """Approve a pending API key (maker-checker — must be a different user)."""
    iid = _get_institution_id(claims)
    uid = _get_user_id(claims)

    row = conn.execute(
        text("""
            SELECT id, requested_by, mc_status
            FROM institution.api_key
            WHERE id = CAST(:kid AS uuid) AND institution_id = CAST(:iid AS uuid)
        """),
        {"kid": key_id, "iid": iid},
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    if row.mc_status != "pending_approval":
        raise HTTPException(status_code=409, detail=f"Key is already {row.mc_status}.")
    if str(row.requested_by) == uid:
        raise HTTPException(
            status_code=403,
            detail="Maker-checker: approver must be different from requester.",
        )

    conn.execute(
        text("""
            UPDATE institution.api_key
            SET mc_status   = 'approved',
                active      = true,
                approved_by = CAST(:uid AS uuid),
                approved_at = now()
            WHERE id = CAST(:kid AS uuid)
        """),
        {"kid": key_id, "uid": uid},
    )
    conn.commit()
    return {"status": "approved", "key_id": key_id}


@router.post("/{key_id}/revoke")
def revoke_key(
    key_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    """Immediately revoke an API key. Irreversible — create a new key to replace."""
    iid = _get_institution_id(claims)
    uid = _get_user_id(claims)

    result = conn.execute(
        text("""
            UPDATE institution.api_key
            SET active     = false,
                revoked_at = now(),
                revoked_by = CAST(:uid AS uuid)
            WHERE id              = CAST(:kid AS uuid)
              AND institution_id  = CAST(:iid AS uuid)
              AND revoked_at      IS NULL
            RETURNING id
        """),
        {"kid": key_id, "iid": iid, "uid": uid},
    ).fetchone()

    if result is None:
        raise HTTPException(status_code=404, detail="API key not found or already revoked.")

    conn.commit()
    return {"status": "revoked", "key_id": key_id}


@router.delete("/{key_id}", status_code=204, response_model=None)
def delete_key(
    key_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
):
    """Delete a pending or revoked key entirely. Cannot delete active keys."""
    iid = _get_institution_id(claims)

    result = conn.execute(
        text("""
            DELETE FROM institution.api_key
            WHERE id             = CAST(:kid AS uuid)
              AND institution_id = CAST(:iid AS uuid)
              AND (mc_status = 'pending_approval' OR revoked_at IS NOT NULL)
            RETURNING id
        """),
        {"kid": key_id, "iid": iid},
    ).fetchone()

    if result is None:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete: key is active. Revoke it first.",
        )
    conn.commit()
