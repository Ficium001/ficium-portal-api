"""Nav badges in one request; fewer DB round trips per request."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app import deps  # noqa: E402
from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

INST = "31b3ee32-2864-4875-8920-cc5f27240971"


class _SP:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def commit(self) -> None:
        self.log.append("commit")

    def rollback(self) -> None:
        self.log.append("rollback")


class Conn:
    def __init__(self) -> None:
        self.sp: list[str] = []

    def begin_nested(self) -> _SP:
        return _SP(self.sp)


@contextmanager
def client(modules: list[str], **lists: Any):
    conn = Conn()

    async def claims() -> dict:
        return {"sub": "u1", "institution_id": INST, "user_role": "checker"}

    def tc():
        yield conn

    defaults = {
        "requests": [{"id": 1}, {"id": 2}],
        "bids": [{"status": "submitted"}, {"status": "accepted"}],
        "pipelines": {"active": [{}, {}, {}], "completed": [{"booked": True}, {"booked": False}]},
        "envelopes": [{"status": "sent"}, {"status": "completed"}],
        "pending": {"bids": [{"action_status": "pending"}], "internal": []},
        "inbox": [{"my_vote": None}, {"my_vote": "approve"}],
    }
    defaults.update(lists)

    async def pipes(status: str, claims: Any, conn: Any) -> list:
        return defaults["pipelines"][status]

    async def pend(scope: str, conn: Any, claims: Any) -> list:
        v = defaults["pending"]
        if isinstance(v, Exception):
            raise v
        return v[scope]

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = tc
    try:
        with patch.object(deps, "_institution_modules", return_value=modules), \
             patch("app.api.portal_badges._institution_modules", return_value=modules), \
             patch("app.api.marketplace.list_requests",
                   AsyncMock(return_value=defaults["requests"])), \
             patch("app.api.marketplace.list_my_bids", AsyncMock(return_value=defaults["bids"])), \
             patch("app.api.pipeline.list_pipelines", pipes), \
             patch("app.api.esign.list_envelopes", AsyncMock(return_value=defaults["envelopes"])), \
             patch("app.api.approvals.list_pending_actions", pend), \
             patch("app.api.approval_engine.my_inbox", AsyncMock(return_value=defaults["inbox"])):
            yield TestClient(app, raise_server_exceptions=False), conn
    finally:
        app.dependency_overrides.clear()


def test_counts_match_the_list_screens_filters() -> None:
    with client(["pipeline"]) as (c, _):
        r = c.get("/portal/badges")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "open_requests": 2, "live_bids": 1, "active_deals": 3, "awaiting_booking": 1,
        "awaiting_signature": 1, "pending_bid_actions": 1, "pending_internal": 0,
        "awaiting_my_vote": 1,
    }


def test_unlicensed_pipeline_gives_null_not_a_count() -> None:
    with client([]) as (c, _):
        body = c.get("/portal/badges").json()
    assert body["active_deals"] is None and body["awaiting_booking"] is None
    assert body["open_requests"] == 2


def test_forbidden_queue_is_null_and_others_survive() -> None:
    with client(["pipeline"]) as (c, conn), \
         patch("app.api.esign.list_envelopes", AsyncMock(side_effect=HTTPException(403))):
        body = c.get("/portal/badges").json()
    assert body["awaiting_signature"] is None
    assert body["live_bids"] == 1
    assert "rollback" in conn.sp


def test_broken_queue_rolls_back_its_savepoint_only() -> None:
    with client(["pipeline"], pending=RuntimeError("db down")) as (c, conn):
        body = c.get("/portal/badges").json()
    assert body["pending_bid_actions"] is None and body["pending_internal"] is None
    assert body["awaiting_my_vote"] == 1
    assert conn.sp.count("rollback") == 2


def test_module_licensing_is_cached() -> None:
    class S:
        calls = 0

        def execute(self, *_a: Any, **_k: Any) -> Any:
            S.calls += 1

            class R:
                def fetchone(self) -> Any:
                    return type("Row", (), {"modules": ["pipeline"]})()
            return R()

    @contextmanager
    def svc():
        yield S()

    with patch("app.deps.service_session", svc):
        for _ in range(5):
            assert deps._institution_modules(INST) == ["pipeline"]
    assert S.calls == 1


def test_tenant_session_sets_claims_and_role_in_one_statement() -> None:
    from app.core import db

    seen: list[str] = []

    class Sess:
        def execute(self, stmt: Any, params: Any = None) -> None:
            seen.append(str(stmt))

        def commit(self) -> None: ...

        def rollback(self) -> None: ...

        def close(self) -> None: ...

    with patch.object(db, "_session_or_raise", return_value=Sess):
        with db.tenant_session({"sub": "u"}):
            pass
    assert len(seen) == 1
    assert "set_config('role', 'authenticated', true)" in seen[0]
    assert "request.jwt.claims" in seen[0]


def test_own_pending_items_do_not_count_as_needing_me() -> None:
    mine = {"action_status": "pending", "is_mine": True}
    theirs = {"action_status": "pending", "is_mine": False}
    queues = {"bids": [mine, theirs, theirs], "internal": [mine]}
    with client(["pipeline"], pending=queues) as (c, _):
        body = c.get("/portal/badges").json()
    assert body["pending_bid_actions"] == 2 and body["pending_internal"] == 0
