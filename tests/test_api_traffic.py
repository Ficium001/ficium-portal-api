# ruff: noqa: E501
"""API traffic (db/064): recorder, pause policy, inbound guard, outbound guards, admin ops."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import internal_admin
from app.core import api_traffic as t
from app.core.config import settings
from app.main import app
from tests.test_internal_admin import CHECKER, KEY, MAKER, FakeConn, _Res, call, cmd, staff


@pytest.fixture(autouse=True)
def clean():
    t.policy.clear()
    t.recorder.drain()
    yield
    t.policy.clear()
    t.recorder.drain()


# ── classification ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,method,expected", [
    ("/internal/admin/v1/commands", "POST", "admin.channel"),
    ("/integration/v1/events", "POST", "integration.contract_in"),
    ("/v1/loans", "GET", "public.v1_in"),
    ("/v1", "GET", "public.v1_in"),
    ("/v1beta/x", "GET", "portal.web_in"),
    ("/marketplace/bids", "GET", "portal.web_in"),
    ("/health", "GET", None),
    ("/metrics", "GET", None),
    ("/anything", "OPTIONS", None),
])
def test_inbound_channel(path: str, method: str, expected: str | None) -> None:
    assert t.inbound_channel(path, method) == expected


def test_every_inbound_result_is_a_known_inbound_channel() -> None:
    for path in ("/internal/admin/v1/x", "/integration/v1/x", "/v1/x", "/x"):
        ch = t.inbound_channel(path)
        assert ch in t.CHANNELS and t.CHANNELS[ch].direction == "inbound"


def test_locked_channels_are_the_two_that_must_never_stop() -> None:
    assert {k for k, c in t.CHANNELS.items() if not c.pausable} == {"admin.channel", "portal.web_in"}


# ── recorder ────────────────────────────────────────────────────────────────────

def test_recorder_counts_outcomes_and_buckets() -> None:
    r = t.Recorder()
    now = 1_700_000_000.0
    r.record("public.v1_in", t.OK, 20, now=now)
    r.record("public.v1_in", t.OK, 700, now=now)
    r.record("public.v1_in", t.CLIENT, 5000, now=now)
    r.record("public.v1_in", t.SERVER, 9000, "HTTP 500", now=now)
    r.record("public.v1_in", t.BLOCKED, None, now=now)
    rows, ok_at, err = r.drain()
    (row,) = rows.values()
    assert row["total"] == 5 and row["ok"] == 2 and row["client_errors"] == 1
    assert row["server_errors"] == 1 and row["blocked"] == 1
    assert row["lat_b0"] == 1 and row["lat_b4"] == 1 and row["lat_b6"] == 1 and row["lat_b7"] == 1
    assert "public.v1_in" in ok_at and err["public.v1_in"][1] == "HTTP 500"
    assert r.drain() == ({}, {}, {})


def test_recorder_ignores_unknown_channels_and_client_errors_are_not_failures() -> None:
    r = t.Recorder()
    r.record("nope", t.OK, 1)
    r.record("public.v1_in", t.CLIENT, 1)
    rows, _, err = r.drain()
    assert len(rows) == 1 and err == {}


def test_recorder_error_text_is_capped() -> None:
    r = t.Recorder()
    r.record("mail.out", t.NETWORK, 1, "x" * 5000)
    _, _, err = r.drain()
    assert len(err["mail.out"][1]) == 300


def test_restore_puts_back_what_a_failed_flush_could_not_write() -> None:
    r = t.Recorder()
    r.record("mail.out", t.OK, 10)
    drained = r.drain()
    r.record("mail.out", t.OK, 10)
    r.restore(*drained)
    rows, _, _ = r.drain()
    assert next(iter(rows.values()))["total"] == 2


def test_flush_failure_keeps_the_counts() -> None:
    r = t.Recorder()
    r.record("mail.out", t.OK, 10)

    @contextmanager
    def boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    with patch.object(t, "service_session", boom):
        assert t.flush(r) == 0
    rows, _, _ = r.drain()
    assert next(iter(rows.values()))["total"] == 1


def test_percentiles_are_bucket_upper_bounds() -> None:
    assert t._percentile_ms([0] * 8, 0.5) is None
    assert t._percentile_ms([10, 0, 0, 0, 0, 0, 0, 0], 0.95) == 50
    assert t._percentile_ms([5, 5, 0, 0, 0, 0, 0, 0], 0.95) == 100
    assert t._percentile_ms([0, 0, 0, 0, 0, 0, 0, 3], 0.5) == t._OVERFLOW_MS


# ── policy ──────────────────────────────────────────────────────────────────────

def test_policy_pauses_only_pausable_channels_and_ignores_stale_versions() -> None:
    t.policy.set("public.v1_in", "paused", 2)
    t.policy.set("public.v1_in", "enabled", 1)   # older: ignored
    assert t.paused("public.v1_in")
    t.policy.set("admin.channel", "paused", 9)    # locked: never enforced
    t.policy.set("portal.web_in", "paused", 9)
    assert not t.paused("admin.channel") and not t.paused("portal.web_in")
    t.policy.set("public.v1_in", "enabled", 3)
    assert not t.paused("public.v1_in")


def test_refresh_keeps_last_known_state_when_db_is_down() -> None:
    t.policy.set("mail.out", "paused", 4)

    @contextmanager
    def boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    with patch.object(t, "service_session", boom):
        t.refresh_policy()
    assert t.paused("mail.out")


# ── inbound guard ───────────────────────────────────────────────────────────────

def test_paused_inbound_channel_answers_503_and_is_counted_as_blocked() -> None:
    t.policy.set("public.v1_in", "paused", 1)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/v1/anything")
    assert r.status_code == 503 and r.json()["error"] == "channel_paused"
    assert r.headers["retry-after"] == t.RETRY_AFTER_S
    rows, _, err = t.recorder.drain()
    row = next(v for (ch, _h), v in rows.items() if ch == "public.v1_in")
    assert row["blocked"] == 1 and err == {}


def test_other_channels_keep_working_while_one_is_paused() -> None:
    t.policy.set("public.v1_in", "paused", 1)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/health").status_code != 503
    r = c.post("/integration/v1/events", content=b"{}")
    assert r.status_code != 503 or r.json().get("error") != "channel_paused"


def test_normal_requests_are_recorded_with_their_outcome() -> None:
    c = TestClient(app, raise_server_exceptions=False)
    c.get("/v1/does-not-exist")
    rows, _, _ = t.recorder.drain()
    row = next(v for (ch, _h), v in rows.items() if ch == "public.v1_in")
    assert row["total"] == 1 and (row["client_errors"] == 1 or row["server_errors"] == 1)


def test_health_is_not_counted() -> None:
    TestClient(app, raise_server_exceptions=False).get("/health")
    assert t.recorder.drain()[0] == {}


# ── outbound guards ─────────────────────────────────────────────────────────────

def test_dispatcher_claims_nothing_while_paused() -> None:
    from app.integration import dispatcher

    t.policy.set("integration.events_out", "paused", 1)
    with patch.object(dispatcher, "_claim") as claim:
        counts = asyncio.run(dispatcher.dispatch_once(client=None))  # type: ignore[arg-type]
    claim.assert_not_called()
    assert counts == {"delivered": 0, "pending": 0, "dead": 0}


def test_mail_refuses_while_paused_and_counts_it() -> None:
    from app.core import mail

    t.policy.set("mail.out", "paused", 1)
    with pytest.raises(mail.EmailNotSent):
        asyncio.run(mail.send_email("a@b.mu", "s", "h"))
    rows, _, _ = t.recorder.drain()
    assert next(iter(rows.values()))["blocked"] == 1


def test_webhook_hold_does_not_touch_failure_counter() -> None:
    from app.core import webhooks

    conn = FakeConn()

    @contextmanager
    def svc():
        yield conn

    t.policy.set("webhooks.out", "paused", 1)
    wh = {"id": uuid.uuid4(), "endpoint_url": "https://x.example", "signing_secret": "s",
          "timeout_ms": 1000, "retry_max": 1}
    with patch.object(webhooks, "service_session", svc):
        asyncio.run(webhooks._fire_single(wh, str(uuid.uuid4()), "bid.created", str(uuid.uuid4()), {}))
    sql = " ".join(q for q, _ in conn.sql)
    assert "INSERT INTO institution.webhook_delivery" in sql
    assert "UPDATE institution.webhook" not in sql and "failure_count" not in sql.replace("webhook_delivery", "")


# ── admin ops ───────────────────────────────────────────────────────────────────

class PolicyConn(FakeConn):
    def __init__(self) -> None:
        super().__init__()
        self.version = 0

    def execute(self, stmt: Any, params: dict | None = None) -> _Res:
        q, p = " ".join(str(stmt).split()), params or {}
        if "INSERT INTO integration.api_channel_policy" in q:
            self.sql.append((q, p))
            if p["v"] > self.version:
                self.version = p["v"]
                return _Res([SimpleNamespace(version=p["v"])])
            return _Res([])
        if "FROM integration.api_traffic_hour" in q or "FROM integration.api_channel_" in q:
            self.sql.append((q, p))
            return _Res([])
        return super().execute(stmt, params)

    def actions(self) -> list[str]:
        return [p["action"] for q, p in self.sql if "INSERT INTO audit.event" in q]


@contextmanager
def chan(conn: PolicyConn):
    @contextmanager
    def svc():
        yield conn

    with (
        patch.object(settings, "admin_command_verify_keys", KEY.decode()),
        patch.object(internal_admin, "service_session", svc),
        patch.object(internal_admin, "verify_token", AsyncMock(return_value=staff())),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def pause(ch: str = "public.v1_in", version: int = 1, maker: str = CHECKER) -> dict:
    return cmd("api_channel.pause", {"channel": ch, "state": "paused", "version": version,
                                     "reason": "Provider incident"}, maker=maker)


def resume(ch: str = "public.v1_in", version: int = 2, maker: str = MAKER) -> dict:
    return cmd("api_channel.resume", {"channel": ch, "state": "enabled", "version": version,
                                      "reason": "Provider recovered"}, maker=maker)


def test_pause_is_one_person_applies_at_once_and_is_audited() -> None:
    conn = PolicyConn()
    with chan(conn) as c:
        r = call(c, "commands", pause())
    assert r.status_code == 200, r.text
    assert r.json()["data"]["applied"] is True
    assert t.paused("public.v1_in")
    assert conn.actions() == ["api_channel.paused"]


def test_pause_maker_must_be_the_actor() -> None:
    conn = PolicyConn()
    with chan(conn) as c:
        r = call(c, "commands", pause(maker=MAKER))
    assert r.status_code == 422 and not t.paused("public.v1_in")


def test_resume_is_four_eyes() -> None:
    conn = PolicyConn()
    t.policy.set("public.v1_in", "paused", 1)
    with chan(conn) as c:
        same = call(c, "commands", resume(maker=CHECKER))
        ok = call(c, "commands", resume())
    assert same.status_code == 403 and same.json()["error"] == "four_eyes"
    assert ok.status_code == 200 and not t.paused("public.v1_in")


@pytest.mark.parametrize("ch", ["admin.channel", "portal.web_in"])
def test_locked_channels_cannot_be_paused(ch: str) -> None:
    conn = PolicyConn()
    with chan(conn) as c:
        r = call(c, "commands", pause(ch))
    assert r.status_code == 409 and r.json()["error"] == "channel_locked"
    assert not any("INSERT INTO integration.api_channel_policy" in q for q, _ in conn.sql)


@pytest.mark.parametrize("args,code", [
    ({"channel": "nope", "state": "paused", "version": 1}, "unknown_channel"),
    ({"channel": "mail.out", "state": "paused", "version": "x"}, "invalid_field"),
    ({"channel": "mail.out", "state": "paused", "version": 0}, "invalid_field"),
])
def test_bad_arguments_are_refused(args: dict, code: str) -> None:
    conn = PolicyConn()
    with chan(conn) as c:
        r = call(c, "commands", cmd("api_channel.pause", args, maker=CHECKER))
    assert r.status_code == 422 and r.json()["error"] == code


def test_older_version_is_a_no_op() -> None:
    conn = PolicyConn()
    conn.version = 5
    with chan(conn) as c:
        r = call(c, "commands", pause(version=3))
    assert r.status_code == 200 and r.json()["data"]["applied"] is False
    assert not t.paused("public.v1_in")


def test_overview_has_every_channel_with_24_hourly_points() -> None:
    conn = PolicyConn()
    with chan(conn) as c:
        r = call(c, "query", {"op": "traffic.overview", "args": {}})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [ch["key"] for ch in data["channels"]] == list(t.CHANNELS)
    assert all(len(ch["hourly_total"]) == 24 and ch["applied_state"] == "enabled" for ch in data["channels"])


def test_overview_reports_totals_and_applied_state() -> None:
    now = time.time()
    hour = datetime.fromtimestamp(now - now % 3600, UTC)
    row = SimpleNamespace(channel="mail.out", hour=hour, total=10, ok=7, client_errors=1,
                          server_errors=1, network_errors=1, blocked=0, lat_b0=4, lat_b1=0, lat_b2=0,
                          lat_b3=0, lat_b4=0, lat_b5=0, lat_b6=0, lat_b7=6)

    class C(PolicyConn):
        def execute(self, stmt: Any, params: dict | None = None) -> _Res:
            q = " ".join(str(stmt).split())
            if "FROM integration.api_traffic_hour" in q:
                return _Res([row])
            if "FROM integration.api_channel_policy" in q:
                return _Res([SimpleNamespace(channel="mail.out", state="paused", version=3)])
            if "FROM integration.api_channel_status" in q:
                return _Res([SimpleNamespace(channel="mail.out", last_ok_at=hour, last_error_at=hour,
                                             last_error="HTTP 502")])
            return super().execute(stmt, params)

    out = t.overview(C(), now)
    m = next(c for c in out["channels"] if c["key"] == "mail.out")
    assert m["total"] == 10 and m["server_errors"] == 1 and m["network_errors"] == 1
    assert m["applied_state"] == "paused" and m["applied_version"] == 3
    assert m["p50_ms"] == t._OVERFLOW_MS and m["p95_ms"] == t._OVERFLOW_MS and m["last_error"] == "HTTP 502"
    assert m["hourly_total"][-1] == 10 and m["hourly_errors"][-1] == 2


def test_the_op_decides_the_state_not_the_arguments() -> None:
    conn = PolicyConn()
    with chan(conn) as c:
        r = call(c, "commands", cmd("api_channel.pause", {"channel": "mail.out", "state": "enabled",
                                                          "version": 1}, maker=CHECKER))
    assert r.status_code == 200 and r.json()["data"]["state"] == "paused" and t.paused("mail.out")


def test_peer_paused_answer_defers_without_using_an_attempt() -> None:
    import httpx

    from app.integration import dispatcher

    env = {"id": "e1"}
    paused_reply = httpx.Response(503, json={"error": "channel_paused"})
    other_503 = httpx.Response(503, json={"error": "boom"})
    assert dispatcher._peer_paused(paused_reply) and not dispatcher._peer_paused(other_503)
    assert not dispatcher._peer_paused(httpx.Response(503, text="<html>"))

    class Client:
        async def post(self, *a: Any, **k: Any) -> httpx.Response:
            return paused_reply

    with (
        patch.object(dispatcher, "_claim", return_value=[("e1", env)]),
        patch.object(dispatcher.fc, "validate_event"),
        patch.object(dispatcher, "_defer") as defer,
        patch.object(dispatcher, "_mark") as mark,
    ):
        counts = asyncio.run(dispatcher.dispatch_once(Client()))  # type: ignore[arg-type]
    defer.assert_called_once_with("e1")
    mark.assert_not_called()
    assert counts["pending"] == 1 and counts["dead"] == 0
