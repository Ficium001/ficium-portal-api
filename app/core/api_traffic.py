# ruff: noqa: E501  (SQL statements and comments)
# =============================================================================
# ficium-portal-api — API traffic (plat:api-traffic)
#
# Every API call into or out of this service belongs to a CHANNEL. The portal admin site
# (ficium-admin, "API traffic") shows how each channel is doing and can pause one. Three parts:
#
#   recorder    every call is counted in memory per channel per hour (outcome + latency bucket)
#               and flushed every ~10 s with one upsert per channel-hour. Database writes depend on
#               the number of channels, never on request volume.
#   policy      the pause state per channel, written only by the admin command channel and read
#               back every ~5 s by every replica. A channel is paused only if its pause is known:
#               if the database cannot be read, the last known state stays in force.
#   guard       inbound: ApiTrafficMiddleware answers 503 {"error": "channel_paused"} with
#               Retry-After before the request reaches any route. Outbound: each sender asks
#               `paused(channel)` first and holds its work instead of failing it.
#
# What is recorded about a failure is a short, scrubbed reason (a status or an exception class),
# never a body, URL, token or address.
#
# Channels the admin site can never pause (`pausable=False`): the admin command channel, because it
# is the only way to resume anything, and normal portal use, which would lock every bank out.
# They are still recorded. The admin database and apply_policy() both refuse to pause them.
# =============================================================================

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

import structlog
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .db import service_session

log = structlog.get_logger()


@dataclass(frozen=True)
class Channel:
    direction: str  # "inbound" | "outbound"
    pausable: bool


CHANNELS: dict[str, Channel] = {
    "integration.contract_in": Channel("inbound", True),  # /integration/v1/*  (borrower app)
    "public.v1_in": Channel("inbound", True),  # /v1/*              (bank API keys)
    "portal.web_in": Channel("inbound", False),  # everything else    (bank staff)
    "admin.channel": Channel("inbound", False),  # /internal/admin/v1 (admin site)
    "integration.events_out": Channel("outbound", True),  # outbox -> borrower app
    "webhooks.out": Channel("outbound", True),  # bank webhooks
    "mail.out": Channel("outbound", True),  # email provider
}

# Calls that are not business traffic: probes, metrics, docs, CORS preflight.
_EXEMPT_PATHS = frozenset({"/health", "/metrics", "/docs", "/redoc", "/openapi.json"})

OK, CLIENT, SERVER, NETWORK, BLOCKED = (
    "ok",
    "client_error",
    "server_error",
    "network_error",
    "blocked",
)
_COLUMN = {
    OK: "ok",
    CLIENT: "client_errors",
    SERVER: "server_errors",
    NETWORK: "network_errors",
    BLOCKED: "blocked",
}

# Upper bounds in ms of lat_b0..lat_b6; lat_b7 is everything slower.
LATENCY_BOUNDS_MS = (50, 100, 250, 500, 1000, 2500, 5000)
_OVERFLOW_MS = 30_000  # what "slower than 5 s" is reported as: the API's own DB timeout

FLUSH_EVERY_S = 10.0
POLICY_EVERY_S = 5.0
PRUNE_EVERY_S = 3600.0
RETENTION_DAYS = 35
WINDOW_HOURS = 24
RETRY_AFTER_S = "30"


def inbound_channel(path: str, method: str = "GET") -> str | None:
    """Which channel an inbound request belongs to, or None for probes, docs and CORS preflight."""
    if method == "OPTIONS" or path in _EXEMPT_PATHS:
        return None
    if path.startswith("/internal/admin/v1"):
        return "admin.channel"
    if path.startswith("/integration/v1"):
        return "integration.contract_in"
    if path == "/v1" or path.startswith("/v1/"):
        return "public.v1_in"
    return "portal.web_in"


def outcome_for_status(status: int) -> str:
    if status >= 500:
        return SERVER
    if status >= 400:
        return CLIENT
    return OK


def _bucket(ms: float) -> int:
    for i, bound in enumerate(LATENCY_BOUNDS_MS):
        if ms <= bound:
            return i
    return len(LATENCY_BOUNDS_MS)


def _hour(now: float) -> datetime:
    return datetime.fromtimestamp(now - now % 3600, UTC)


# ---------------------------------------------------------------------------
# Policy (what is paused)
# ---------------------------------------------------------------------------


class PolicyCache:
    """The pause state per channel, as last read from the database or set by a command here."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state: dict[str, tuple[str, int]] = {}

    def set(self, channel: str, state: str, version: int) -> None:
        with self._lock:
            cur = self._state.get(channel)
            if cur is None or version >= cur[1]:
                self._state[channel] = (state, version)

    def paused(self, channel: str) -> bool:
        ch = CHANNELS.get(channel)
        if ch is None or not ch.pausable:
            return False
        with self._lock:
            cur = self._state.get(channel)
        return cur is not None and cur[0] == "paused"

    def clear(self) -> None:
        with self._lock:
            self._state.clear()


policy = PolicyCache()


def paused(channel: str) -> bool:
    return policy.paused(channel)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class Recorder:
    """In-memory counters per (channel, hour), drained by flush(). Thread-safe."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._rows: dict[tuple[str, datetime], dict[str, int]] = {}
        self._ok_at: dict[str, datetime] = {}
        self._err: dict[str, tuple[datetime, str]] = {}

    def record(
        self,
        channel: str,
        outcome: str,
        ms: float | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        if channel not in CHANNELS:
            return
        t = time.time() if now is None else now
        key = (channel, _hour(t))
        at = datetime.fromtimestamp(t, UTC)
        with self._lock:
            row = self._rows.setdefault(key, defaultdict(int))
            row["total"] += 1
            row[_COLUMN[outcome]] += 1
            if ms is not None and outcome != BLOCKED:
                row[f"lat_b{_bucket(ms)}"] += 1
            if outcome == OK:
                self._ok_at[channel] = at
            elif outcome in (SERVER, NETWORK):
                self._err[channel] = (at, (error or outcome)[:300])

    def drain(self) -> tuple[dict, dict, dict]:
        with self._lock:
            rows, ok_at, err = self._rows, self._ok_at, self._err
            self._rows, self._ok_at, self._err = {}, {}, {}
        return rows, ok_at, err

    def restore(self, rows: dict, ok_at: dict, err: dict) -> None:
        """Put back what a failed flush could not write, so a database blip loses nothing."""
        with self._lock:
            for key, row in rows.items():
                cur = self._rows.setdefault(key, defaultdict(int))
                for k, v in row.items():
                    cur[k] += v
            for ch, at in ok_at.items():
                if ch not in self._ok_at or at > self._ok_at[ch]:
                    self._ok_at[ch] = at
            for ch, e in err.items():
                if ch not in self._err or e[0] > self._err[ch][0]:
                    self._err[ch] = e


recorder = Recorder()


def record(
    channel: str, outcome: str, started: float | None = None, error: str | None = None
) -> None:
    """Count one call. `started` is time.perf_counter() from before the call, for the latency."""
    ms = None if started is None else (time.perf_counter() - started) * 1000
    recorder.record(channel, outcome, ms, error)


_COUNT_COLS = (
    "total",
    "ok",
    "client_errors",
    "server_errors",
    "network_errors",
    "blocked",
    "lat_b0",
    "lat_b1",
    "lat_b2",
    "lat_b3",
    "lat_b4",
    "lat_b5",
    "lat_b6",
    "lat_b7",
)
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text  (built only from constant column names)
_UPSERT = text(
    "INSERT INTO integration.api_traffic_hour (channel, hour, " + ", ".join(_COUNT_COLS) + ") "
    "VALUES (:channel, :hour, " + ", ".join(f":{c}" for c in _COUNT_COLS) + ") "
    "ON CONFLICT (channel, hour) DO UPDATE SET "
    + ", ".join(f"{c} = integration.api_traffic_hour.{c} + EXCLUDED.{c}" for c in _COUNT_COLS)
)
_STATUS_UPSERT = text("""
    INSERT INTO integration.api_channel_status (channel, last_ok_at, last_error_at, last_error)
    VALUES (:channel, :ok_at, :err_at, :err)
    ON CONFLICT (channel) DO UPDATE SET
      last_ok_at    = GREATEST(integration.api_channel_status.last_ok_at, EXCLUDED.last_ok_at),
      last_error_at = CASE WHEN EXCLUDED.last_error_at IS NOT NULL
                            AND (integration.api_channel_status.last_error_at IS NULL
                                 OR EXCLUDED.last_error_at >= integration.api_channel_status.last_error_at)
                           THEN EXCLUDED.last_error_at ELSE integration.api_channel_status.last_error_at END,
      last_error    = CASE WHEN EXCLUDED.last_error_at IS NOT NULL
                            AND (integration.api_channel_status.last_error_at IS NULL
                                 OR EXCLUDED.last_error_at >= integration.api_channel_status.last_error_at)
                           THEN EXCLUDED.last_error ELSE integration.api_channel_status.last_error END
""")


def flush(rec: Recorder = recorder) -> int:
    """Write everything recorded since the last flush. Returns the number of channel-hours written."""
    rows, ok_at, err = rec.drain()
    if not rows and not ok_at and not err:
        return 0
    try:
        with service_session() as conn:
            for (channel, hour), row in rows.items():
                conn.execute(
                    _UPSERT,
                    {"channel": channel, "hour": hour, **{c: row.get(c, 0) for c in _COUNT_COLS}},
                )
            for channel in set(ok_at) | set(err):
                e = err.get(channel)
                conn.execute(
                    _STATUS_UPSERT,
                    {
                        "channel": channel,
                        "ok_at": ok_at.get(channel),
                        "err_at": e[0] if e else None,
                        "err": e[1] if e else None,
                    },
                )
    except Exception:  # noqa: BLE001 — a database blip must not take the API down
        rec.restore(rows, ok_at, err)
        log.warning("api_traffic_flush_failed", channels=len(rows))
        return 0
    return len(rows)


def refresh_policy(cache: PolicyCache = policy) -> None:
    """Read the stored pause state. On failure the last known state stays in force."""
    try:
        with service_session() as conn:
            rows = conn.execute(
                text("SELECT channel, state, version FROM integration.api_channel_policy")
            ).fetchall()
    except Exception:  # noqa: BLE001
        log.warning("api_traffic_policy_read_failed")
        return
    for r in rows:
        cache.set(r.channel, r.state, int(r.version))


def prune(days: int = RETENTION_DAYS) -> None:
    try:
        with service_session() as conn:
            conn.execute(
                text(
                    "DELETE FROM integration.api_traffic_hour WHERE hour < now() - make_interval(days => :d)"
                ),
                {"d": days},
            )
    except Exception:  # noqa: BLE001
        log.warning("api_traffic_prune_failed")


async def run_forever(stop: asyncio.Event) -> None:
    """Background loop on every replica: keep the pause state fresh, flush counters, prune old hours."""
    log.info("api_traffic_started")
    await asyncio.to_thread(refresh_policy)
    last_flush = last_prune = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLICY_EVERY_S)
        except TimeoutError:
            pass
        await asyncio.to_thread(refresh_policy)
        now = time.monotonic()
        if stop.is_set() or now - last_flush >= FLUSH_EVERY_S:
            await asyncio.to_thread(flush)
            last_flush = now
        if now - last_prune >= PRUNE_EVERY_S:
            await asyncio.to_thread(prune)
            last_prune = now
    await asyncio.to_thread(flush)  # whatever is left when we stop
    log.info("api_traffic_stopped")


# ---------------------------------------------------------------------------
# Inbound guard + recorder
# ---------------------------------------------------------------------------


class ApiTrafficMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        channel = inbound_channel(request.url.path, request.method)
        if channel is None:
            return await call_next(request)
        if paused(channel):
            record(channel, BLOCKED)
            return JSONResponse(
                status_code=503,
                content={
                    "error": "channel_paused",
                    "detail": "This part of the service is paused by Ficium. Try again shortly.",
                },
                headers={"Retry-After": RETRY_AFTER_S, "Cache-Control": "no-store"},
            )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            record(channel, SERVER, started, type(exc).__name__)
            raise
        status = response.status_code
        outcome = outcome_for_status(status)
        record(channel, outcome, started, f"HTTP {status}" if outcome == SERVER else None)
        return response


# ---------------------------------------------------------------------------
# Admin site: what it reads and what it sets
# ---------------------------------------------------------------------------


def _percentile_ms(buckets: list[int], q: float) -> int | None:
    n = sum(buckets)
    if n == 0:
        return None
    target, seen = q * n, 0
    for i, c in enumerate(buckets):
        seen += c
        if seen >= target:
            return LATENCY_BOUNDS_MS[i] if i < len(LATENCY_BOUNDS_MS) else _OVERFLOW_MS
    return _OVERFLOW_MS


def overview(conn: Any, now: float | None = None) -> dict[str, Any]:
    """The last 24 hours per channel, for the admin site. Upper bounds for latency, scrubbed errors."""
    t = time.time() if now is None else now
    first = _hour(t - (WINDOW_HOURS - 1) * 3600)
    hours = [_hour(t - (WINDOW_HOURS - 1 - i) * 3600) for i in range(WINDOW_HOURS)]
    rows = conn.execute(
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text  (built only from constant column names)
        text(
            "SELECT channel, hour, "
            + ", ".join(_COUNT_COLS)
            + " FROM integration.api_traffic_hour WHERE hour >= :first"
        ),
        {"first": first},
    ).fetchall()
    status = {
        r.channel: r
        for r in conn.execute(
            text(
                "SELECT channel, last_ok_at, last_error_at, last_error FROM integration.api_channel_status"
            )
        ).fetchall()
    }
    pol = {
        r.channel: r
        for r in conn.execute(
            text("SELECT channel, state, version FROM integration.api_channel_policy")
        ).fetchall()
    }

    by: dict[str, dict[datetime, Any]] = defaultdict(dict)
    for r in rows:
        by[r.channel][r.hour] = r
    out = []
    for key in CHANNELS:
        per = by.get(key, {})
        agg = {c: sum(int(getattr(r, c)) for r in per.values()) for c in _COUNT_COLS}
        lat = [agg[f"lat_b{i}"] for i in range(len(LATENCY_BOUNDS_MS) + 1)]
        st, p = status.get(key), pol.get(key)
        out.append(
            {
                "key": key,
                "total": agg["total"],
                "ok": agg["ok"],
                "client_errors": agg["client_errors"],
                "server_errors": agg["server_errors"],
                "network_errors": agg["network_errors"],
                "blocked": agg["blocked"],
                "p50_ms": _percentile_ms(lat, 0.50),
                "p95_ms": _percentile_ms(lat, 0.95),
                "last_ok_at": st.last_ok_at.isoformat() if st and st.last_ok_at else None,
                "last_error_at": st.last_error_at.isoformat() if st and st.last_error_at else None,
                "last_error": st.last_error if st else None,
                # What this service is enforcing. No row yet means never changed: enabled, version 0.
                "applied_state": p.state if p else "enabled",
                "applied_version": int(p.version) if p else 0,
                "hourly_total": [int(per[h].total) if h in per else 0 for h in hours],
                "hourly_errors": [
                    int(per[h].server_errors) + int(per[h].network_errors) if h in per else 0
                    for h in hours
                ],
            }
        )
    return {
        "generated_at": datetime.fromtimestamp(t, UTC).isoformat(),
        "window_hours": WINDOW_HOURS,
        "channels": out,
    }


class PolicyError(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status, self.code, self.detail = status, code, detail


def apply_policy(
    conn: Any, args: dict[str, Any], actor: str, cache: PolicyCache = policy
) -> dict[str, Any]:
    """Store a pause or resume sent by the admin site. A version at or below the stored one is a no-op."""
    channel, state, reason = args.get("channel"), args.get("state"), args.get("reason")
    try:
        version = int(args.get("version"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise PolicyError(422, "invalid_field", "version must be a whole number") from None
    ch = CHANNELS.get(str(channel))
    if ch is None:
        raise PolicyError(422, "unknown_channel", "That channel does not exist here")
    if state not in ("enabled", "paused"):
        raise PolicyError(422, "invalid_field", "state must be enabled or paused")
    if state == "paused" and not ch.pausable:
        raise PolicyError(409, "channel_locked", "That channel cannot be paused")
    if version < 1:
        raise PolicyError(422, "invalid_field", "version must be at least 1")
    if reason is not None and not isinstance(reason, str):
        raise PolicyError(422, "invalid_field", "reason must be text")
    row = conn.execute(
        text("""
        INSERT INTO integration.api_channel_policy (channel, state, version, reason, changed_by, changed_at)
        VALUES (:c, :s, :v, :r, CAST(:a AS uuid), now())
        ON CONFLICT (channel) DO UPDATE
           SET state = EXCLUDED.state, version = EXCLUDED.version, reason = EXCLUDED.reason,
               changed_by = EXCLUDED.changed_by, changed_at = now()
         WHERE integration.api_channel_policy.version < EXCLUDED.version
        RETURNING version
    """),
        {
            "c": channel,
            "s": state,
            "v": version,
            "r": (reason or None) and reason[:2000],
            "a": actor,
        },
    ).fetchone()
    applied = row is not None
    if applied:
        cache.set(
            str(channel), str(state), version
        )  # this replica obeys at once; the others within seconds
        log.info("api_channel_policy_applied", channel=channel, state=state, version=version)
    return {"channel": channel, "state": state, "version": version, "applied": applied}
