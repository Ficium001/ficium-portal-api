# =============================================================================
# ficium-portal-api — Webhook dispatcher
#
# Fires signed HTTP POST events to institution-registered endpoints.
# Called from any router that emits a business event.
#
# Delivery contract:
#  - Payload signed with HMAC-SHA256 using institution.webhook.signing_secret
#  - Signature sent as X-Ficium-Signature-256: sha256=<hex>
#  - Timestamp sent as X-Ficium-Timestamp: <unix epoch seconds>
#  - Retry up to webhook.retry_max times with exponential backoff (5s, 25s, 125s)
#  - Delivery record written to institution.webhook_delivery on every attempt
#  - Failed webhook.failure_count incremented; auto-disabled at >= 10 consecutive
#
# Calling pattern:
#   from app.core.webhooks import dispatch_event
#   # inside any router after a successful DB write:
#   asyncio.create_task(dispatch_event(institution_id, "bid.accepted", payload))
#
# Event types (enforced by event_types JSONB on institution.webhook):
#   request.new          — new borrower request posted
#   bid.accepted         — borrower accepted institution's bid
#   bid.rejected         — bid expired / request closed without acceptance
#   pipeline.stage_changed — pipeline stage advanced or completed
#   identity.revealed    — phase-2 borrower identity made available
# =============================================================================

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import service_session

log = structlog.get_logger()

# Seconds between retry attempts: 5, 25, 125
_BACKOFF = [5, 25, 125]
_MAX_FAILURES_BEFORE_DISABLE = 10


def _sign(payload_bytes: bytes, secret: str) -> str:
    """Return HMAC-SHA256 hex signature of the payload."""
    mac = hmac.new(secret.encode(), payload_bytes, hashlib.sha256)
    return mac.hexdigest()


def _get_webhooks_for_event(conn: Session, institution_id: str, event_type: str) -> list[dict]:
    rows = conn.execute(
        text("""
            SELECT id, endpoint_url, signing_secret, retry_max, timeout_ms
            FROM institution.webhook
            WHERE institution_id = :iid
              AND active = true
              AND event_types @> CAST(:et AS jsonb)
              AND failure_count < :max_fail
        """),
        {
            "iid": institution_id,
            "et": json.dumps([event_type]),
            "max_fail": _MAX_FAILURES_BEFORE_DISABLE,
        },
    ).fetchall()
    return [dict(r._mapping) for r in rows]


async def _fire_single(
    webhook: dict,
    event_id: str,
    event_type: str,
    institution_id: str,
    payload: dict,
) -> None:
    """Fire one webhook with retries, writing a delivery record per attempt."""
    payload_bytes = json.dumps(
        {"event_id": event_id, "event_type": event_type, **payload},
        separators=(",", ":"),
    ).encode()

    timestamp = int(time.time())
    sig = _sign(payload_bytes, webhook["signing_secret"])
    headers = {
        "Content-Type": "application/json",
        "X-Ficium-Event": event_type,
        "X-Ficium-Delivery": event_id,
        "X-Ficium-Timestamp": str(timestamp),
        "X-Ficium-Signature-256": f"sha256={sig}",
    }

    delivery_id = str(uuid.uuid4())
    timeout_s = (webhook["timeout_ms"] or 30_000) / 1000
    retry_max = webhook["retry_max"] or 3
    attempts = 0
    status_code = None
    response_body = None
    delivered = False

    for attempt in range(retry_max + 1):
        attempts += 1
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(
                    webhook["endpoint_url"],
                    content=payload_bytes,
                    headers=headers,
                )
            status_code = resp.status_code
            response_body = resp.text[:2000]
            delivered = 200 <= status_code < 300
        except Exception as exc:  # noqa: BLE001
            status_code = None
            response_body = str(exc)[:2000]
            delivered = False

        if delivered:
            break

        if attempt < retry_max:
            backoff = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            log.warning(
                "webhook_retry",
                webhook_id=str(webhook["id"]),
                attempt=attempt + 1,
                backoff_s=backoff,
            )
            await asyncio.sleep(backoff)

    # Write delivery record
    delivery_status = "delivered" if delivered else "failed"
    try:
        with service_session() as conn:
            conn.execute(
                text("""
                    INSERT INTO institution.webhook_delivery
                        (id, webhook_id, institution_id, event_type, event_id,
                         payload, status, attempts, last_attempt_at,
                         response_status, response_body,
                         delivered_at)
                    VALUES
                        (:id, :wid, :iid, :et, CAST(:eid AS uuid),
                         CAST(:payload AS jsonb), :status, :attempts, now(),
                         :rsc, :rb,
                         CASE WHEN :delivered THEN now() ELSE NULL END)
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": delivery_id,
                    "wid": str(webhook["id"]),
                    "iid": institution_id,
                    "et": event_type,
                    "eid": event_id,
                    "payload": json.dumps(payload),
                    "status": delivery_status,
                    "attempts": attempts,
                    "rsc": status_code,
                    "rb": response_body,
                    "delivered": delivered,
                },
            )
            # Update webhook metadata
            if delivered:
                conn.execute(
                    text("""
                        UPDATE institution.webhook
                        SET last_fired_at  = now(),
                            last_status    = 'success',
                            failure_count  = 0
                        WHERE id = :wid
                    """),
                    {"wid": str(webhook["id"])},
                )
            else:
                conn.execute(
                    text("""
                        UPDATE institution.webhook
                        SET last_fired_at  = now(),
                            last_status    = 'failed',
                            failure_count  = failure_count + 1,
                            active = CASE WHEN failure_count + 1 >= :max THEN false ELSE active END
                        WHERE id = :wid
                    """),
                    {"wid": str(webhook["id"]), "max": _MAX_FAILURES_BEFORE_DISABLE},
                )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("webhook_delivery_record_failed", webhook_id=str(webhook["id"]))

    log.info(
        "webhook_fired",
        webhook_id=str(webhook["id"]),
        event_type=event_type,
        status=delivery_status,
        attempts=attempts,
        http_status=status_code,
    )


async def dispatch_event(
    institution_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """
    Dispatch a webhook event to all active webhooks subscribed to event_type.
    Should be called with asyncio.create_task() so it doesn't block the response.

    Example:
        import asyncio
        asyncio.create_task(dispatch_event(institution_id, "bid.accepted", {
            "bid_id": bid_id, "request_id": request_id
        }))
    """
    event_id = str(uuid.uuid4())

    try:
        with service_session() as conn:
            webhooks = _get_webhooks_for_event(conn, institution_id, event_type)
    except Exception:  # noqa: BLE001
        log.exception("webhook_lookup_failed", institution_id=institution_id, event_type=event_type)
        return

    if not webhooks:
        return

    log.info(
        "webhook_dispatching",
        institution_id=institution_id,
        event_type=event_type,
        event_id=event_id,
        webhooks=len(webhooks),
    )

    await asyncio.gather(
        *[_fire_single(wh, event_id, event_type, institution_id, payload) for wh in webhooks],
        return_exceptions=True,
    )
