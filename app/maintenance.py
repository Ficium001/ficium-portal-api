"""Scheduled maintenance that runs inside portal-api.

Close expired bid windows every `maintenance_interval_s` seconds. Safe with several replicas:
the work itself takes a transaction-scoped advisory lock (see marketplace.close_expired_once).
OFF unless MAINTENANCE_CLOSE_EXPIRED_ENABLED=true.
"""

from __future__ import annotations

import asyncio

import structlog

from .api.marketplace import close_expired_once
from .core.config import settings

log = structlog.get_logger()


async def run_once() -> dict[str, int] | None:
    try:
        counts = await asyncio.to_thread(close_expired_once)
    except Exception:  # noqa: BLE001 - a DB blip must not kill the loop
        log.exception("maintenance_close_expired_error")
        return None
    if counts["closed"] or counts["expired"]:
        log.info("maintenance_close_expired", **counts)
    return counts


async def run_forever(stop: asyncio.Event) -> None:
    log.info("maintenance_started", interval_s=settings.maintenance_interval_s)
    while not stop.is_set():
        await run_once()
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.maintenance_interval_s)
        except TimeoutError:
            pass
    log.info("maintenance_stopped")
