# =============================================================================
# ficium-portal-api — Webhook management router
#
# Endpoints (JWT-authenticated portal users only):
#
#   GET    /webhooks                   list webhooks for institution
#   POST   /webhooks                   register a new webhook
#   PUT    /webhooks/{id}              update a webhook
#   DELETE /webhooks/{id}             delete a webhook
#   POST   /webhooks/{id}/test         fire a test ping event
#   GET    /webhooks/{id}/deliveries   delivery log (paginated)
#   POST   /webhooks/{id}/reset-failures  clear failure_count, re-enable
#
# Supported event_types:
#   request.new | bid.accepted | bid.rejected
#   pipeline.stage_changed | identity.revealed
# =============================================================================

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.ssrf import validate_webhook_url, WebhookUrlError

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_VALID_EVENTS = {
    "request.new",
    "bid.accepted",
    "bid.rejected",
    "pipeline.stage_changed",
    "identity.revealed",
}


def _get_institution_id(claims: dict[str, Any]) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="No institution context.")
    return str(iid)


class WebhookCreateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    endpoint_url: HttpUrl
    event_types: list[str] = Field(default_factory=list)
    retry_max: int = Field(default=3, ge=0, le=10)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=120_000)


class WebhookUpdateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    endpoint_url: HttpUrl | None = None
    event_types: list[str] | None = None
    active: bool | None = None
    retry_max: int | None = Field(default=None, ge=0, le=10)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=120_000)


def _generate_signing_secret() -> str:
    """Generate a 32-byte HMAC signing secret (shown once on creation)."""
    return "whsec_" + secrets.token_hex(32)


def _hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@router.get("")
def list_webhooks(
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> list[dict]:
    iid = _get_institution_id(claims)
    rows = conn.execute(
        text("""
            SELECT
                id, label, endpoint_url, event_types, active,
                retry_max, timeout_ms, failure_count,
                last_fired_at, last_status, created_at, updated_at
            FROM institution.webhook
            WHERE institution_id = CAST(:iid AS uuid)
            ORDER BY created_at DESC
        """),
        {"iid": iid},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("", status_code=201)
def create_webhook(
    body: Annotated[WebhookCreateRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    """
    Register a webhook endpoint.
    Returns the signing_secret ONCE — store it to verify incoming payloads.
    """
    iid = _get_institution_id(claims)

    bad = [e for e in body.event_types if e not in _VALID_EVENTS]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown event types: {bad}")

    # SSRF guard: reject internal/metadata/private targets before storing.
    try:
        validate_webhook_url(str(body.endpoint_url))
    except WebhookUrlError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    raw_secret = _generate_signing_secret()

    row = conn.execute(
        text("""
            INSERT INTO institution.webhook
                (institution_id, label, endpoint_url, signing_secret,
                 event_types, active, retry_max, timeout_ms)
            VALUES
                (CAST(:iid AS uuid), :label, :url, :secret,
                 CAST(:events AS jsonb), true, :retry, :timeout)
            RETURNING id, label, endpoint_url, event_types, active,
                      retry_max, timeout_ms, created_at
        """),
        {
            "iid": iid,
            "label": body.label,
            "url": str(body.endpoint_url),
            "secret": json.dumps(list(body.event_types)),
            "retry": body.retry_max,
            "timeout": body.timeout_ms,
            # We store the raw secret directly (it IS the secret, not a hash)
            # Bank can rotate it via delete+recreate or a future rotate endpoint
            "secret": raw_secret,
        },
    ).fetchone()
    conn.commit()

    return {
        **dict(row._mapping),
        "signing_secret": raw_secret,
        "warning": "This is the only time the signing secret will be shown. Store it to verify webhook signatures.",
    }


@router.put("/{webhook_id}")
def update_webhook(
    webhook_id: str,
    body: Annotated[WebhookUpdateRequest, Body()],
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    iid = _get_institution_id(claims)

    existing = conn.execute(
        text("SELECT id FROM institution.webhook WHERE id = CAST(:wid AS uuid) AND institution_id = CAST(:iid AS uuid)"),
        {"wid": webhook_id, "iid": iid},
    ).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")

    if body.event_types is not None:
        bad = [e for e in body.event_types if e not in _VALID_EVENTS]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown event types: {bad}")

    updates = []
    params: dict[str, Any] = {"wid": webhook_id, "iid": iid}

    if body.label is not None:
        updates.append("label = :label"); params["label"] = body.label
    if body.endpoint_url is not None:
        try:
            validate_webhook_url(str(body.endpoint_url))
        except WebhookUrlError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        updates.append("endpoint_url = :url"); params["url"] = str(body.endpoint_url)
    if body.event_types is not None:
        updates.append("event_types = CAST(:events AS jsonb)")
        params["events"] = json.dumps(body.event_types)
    if body.active is not None:
        updates.append("active = :active"); params["active"] = body.active
    if body.retry_max is not None:
        updates.append("retry_max = :retry"); params["retry"] = body.retry_max
    if body.timeout_ms is not None:
        updates.append("timeout_ms = :timeout"); params["timeout"] = body.timeout_ms

    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update.")

    updates.append("updated_at = now()")

    row = conn.execute(
        text(f"""
            UPDATE institution.webhook
            SET {', '.join(updates)}
            WHERE id = CAST(:wid AS uuid) AND institution_id = CAST(:iid AS uuid)
            RETURNING id, label, endpoint_url, event_types, active,
                      retry_max, timeout_ms, failure_count, updated_at
        """),
        params,
    ).fetchone()
    conn.commit()
    return dict(row._mapping)


@router.delete("/{webhook_id}", status_code=204, response_model=None)
def delete_webhook(
    webhook_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
):
    iid = _get_institution_id(claims)
    result = conn.execute(
        text("""
            DELETE FROM institution.webhook
            WHERE id = CAST(:wid AS uuid) AND institution_id = CAST(:iid AS uuid)
            RETURNING id
        """),
        {"wid": webhook_id, "iid": iid},
    ).fetchone()
    if result is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    conn.commit()


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    """Fire a synthetic ping event to verify the endpoint is reachable."""
    iid = _get_institution_id(claims)

    row = conn.execute(
        text("""
            SELECT id, endpoint_url, signing_secret, retry_max, timeout_ms
            FROM institution.webhook
            WHERE id = CAST(:wid AS uuid) AND institution_id = CAST(:iid AS uuid)
        """),
        {"wid": webhook_id, "iid": iid},
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")

    from ..core.webhooks import _fire_single
    await _fire_single(
        dict(row._mapping),
        event_id="00000000-0000-0000-0000-000000000000",
        event_type="ping",
        institution_id=iid,
        payload={"message": "Ficium webhook test ping"},
    )
    return {"status": "test_fired", "webhook_id": webhook_id}


@router.get("/{webhook_id}/deliveries")
def list_deliveries(
    webhook_id: str,
    limit: int = Query(default=25, le=100),
    offset: int = Query(default=0, ge=0),
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    iid = _get_institution_id(claims)

    # Verify webhook belongs to institution
    exists = conn.execute(
        text("SELECT id FROM institution.webhook WHERE id = CAST(:wid AS uuid) AND institution_id = CAST(:iid AS uuid)"),
        {"wid": webhook_id, "iid": iid},
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")

    rows = conn.execute(
        text("""
            SELECT
                id, event_type, event_id, status, attempts,
                last_attempt_at, response_status, response_body,
                delivered_at, created_at
            FROM institution.webhook_delivery
            WHERE webhook_id = CAST(:wid AS uuid)
            ORDER BY created_at DESC
            LIMIT :lim OFFSET :off
        """),
        {"wid": webhook_id, "lim": limit, "off": offset},
    ).fetchall()

    total = conn.execute(
        text("SELECT COUNT(*) FROM institution.webhook_delivery WHERE webhook_id = CAST(:wid AS uuid)"),
        {"wid": webhook_id},
    ).scalar()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "deliveries": [dict(r._mapping) for r in rows],
    }


@router.post("/{webhook_id}/reset-failures")
def reset_failures(
    webhook_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict:
    """Clear the failure counter and re-enable a disabled webhook."""
    iid = _get_institution_id(claims)
    result = conn.execute(
        text("""
            UPDATE institution.webhook
            SET failure_count = 0, active = true, updated_at = now()
            WHERE id = CAST(:wid AS uuid) AND institution_id = CAST(:iid AS uuid)
            RETURNING id
        """),
        {"wid": webhook_id, "iid": iid},
    ).fetchone()
    if result is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    conn.commit()
    return {"status": "reset", "webhook_id": webhook_id}
