"""Deliver integration.outbox rows to the borrower side, signed (I2B key).

Runs as a background task in every replica. claim_batch() leases rows with
FOR UPDATE SKIP LOCKED, so replicas never send the same row at once; a row
whose lease expires (replica died mid-send) is picked up again.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import ficium_contract as fc
import httpx
import structlog
from sqlalchemy import text

from ..core.config import settings
from ..core.db import service_session

log = structlog.get_logger()


def _claim(limit: int) -> list[tuple[str, dict[str, Any]]]:
    with service_session() as s:
        rows = s.execute(
            text("SELECT id, envelope FROM integration.claim_batch(:n, 60)"), {"n": limit}
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _mark(event_id: str, ok: bool, error: str) -> str:
    with service_session() as s:
        if ok:
            s.execute(text("SELECT integration.mark_delivered(:i)"), {"i": event_id})
            return "delivered"
        return str(
            s.execute(
                text("SELECT integration.mark_failed(:i, :e)"), {"i": event_id, "e": error}
            ).scalar_one()
        )


def encode(envelope: dict[str, Any]) -> bytes:
    """The exact bytes that are signed and sent."""
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def dispatch_once(client: httpx.AsyncClient, limit: int = 20) -> dict[str, int]:
    key = settings.integration_i2b_signing_key.encode()
    url = settings.integration_peer_url
    counts = {"delivered": 0, "pending": 0, "dead": 0}
    rows = await asyncio.to_thread(_claim, limit)
    for event_id, envelope in rows:
        body = encode(envelope)
        try:
            r = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    fc.SIGNATURE_HEADER: fc.sign(body, key),
                },
                timeout=10.0,
            )
            ok, err = 200 <= r.status_code < 300, f"HTTP {r.status_code}: {r.text[:300]}"
        except httpx.HTTPError as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        outcome = await asyncio.to_thread(_mark, event_id, ok, err)
        counts[outcome] = counts.get(outcome, 0) + 1
        if not ok:
            log.warning(
                "integration_delivery_failed",
                event_id=event_id,
                type=envelope.get("type"),
                outcome=outcome,
                error=err,
            )
    return counts


async def run_forever(stop: asyncio.Event) -> None:
    log.info("integration_dispatcher_started", peer=settings.integration_peer_url)
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                counts = await dispatch_once(client)
                if any(counts.values()):
                    log.info("integration_dispatch", **counts)
            except Exception:  # noqa: BLE001 — a DB blip must not kill the loop
                log.exception("integration_dispatch_error")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.integration_dispatch_interval_s
                )
            except TimeoutError:
                pass
    log.info("integration_dispatcher_stopped")
