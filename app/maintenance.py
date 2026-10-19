"""Scheduled maintenance that runs inside portal-api.

Every `maintenance_interval_s` seconds: close expired bid windows (each closed app request emits
request.window_closed to the borrower side). At most hourly: publish market.intelligence (skipped by
the database when nothing changed). Both go through the contract outbox: no direct call to the
borrower app. Safe with several replicas: closing takes a transaction-scoped advisory lock (see
marketplace.close_expired_once); a duplicate market snapshot is skipped as unchanged.
OFF unless MAINTENANCE_CLOSE_EXPIRED_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from sqlalchemy import text

from .api.marketplace import close_expired_once
from .core.config import settings
from .core.db import service_session

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


MARKET_EVERY_S = 3600
_last_market = 0.0


def _publish_market() -> bool:
    with service_session() as conn:
        row = conn.execute(text("SELECT integration.publish_market_intelligence()"))
        return bool(row.scalar_one())


async def publish_market_if_due(now: float | None = None) -> bool | None:
    """market.intelligence at most hourly. True = a new snapshot was enqueued."""
    global _last_market
    now = time.monotonic() if now is None else now
    if _last_market and now - _last_market < MARKET_EVERY_S:
        return None
    _last_market = now
    try:
        published = await asyncio.to_thread(_publish_market)
    except Exception:  # noqa: BLE001 - a DB blip must not kill the loop
        log.exception("maintenance_market_intelligence_error")
        return None
    if published:
        log.info("maintenance_market_intelligence_published")
    return published


async def run_forever(stop: asyncio.Event) -> None:
    log.info("maintenance_started", interval_s=settings.maintenance_interval_s)
    while not stop.is_set():
        await run_once()
        await publish_market_if_due()
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.maintenance_interval_s)
        except TimeoutError:
            pass
    log.info("maintenance_stopped")
