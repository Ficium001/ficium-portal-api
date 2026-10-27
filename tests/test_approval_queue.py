"""Approval queue: expired is its own state, never 'pending'; own items never
count as 'needs you'. Reads derive the expired state in SQL (the sweep that
persists it is db/054)."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchall(self) -> list[Any]:
        return self.rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def scalar_one(self) -> Any:
        return self.rows[0]


class FakeConn:
    """Records every statement; answers by a keyword the statement contains."""

    def __init__(self, answers: dict[str, list[Any]]) -> None:
        self.answers = answers
        self.sql: list[tuple[str, dict]] = []

    def execute(self, stmt: Any, params: dict | None = None) -> _Result:
        q = str(stmt)
        self.sql.append((q, params or {}))
        for key, rows in self.answers.items():
            if key in q:
                return _Result(rows)
        return _Result([])


def _row(**kw: Any) -> Any:
    base = dict(
        id="a1", action_category="bid.submit", action_status="pending", scope="institution",
        label=None, risk="medium", institution_id="i1", maker_id=OTHER, maker_role="maker",
        resource_type="institution_bids", resource_id=None, resource_label=None, payload={},
        payload_before=None, checker_id=None, checker_role=None, checker_note=None,
        checked_at=None, execution_status="pending", executed_at=None, execution_error=None,
        expires_at=datetime(2026, 10, 30, tzinfo=UTC),
        initiated_at=datetime(2026, 10, 1, tzinfo=UTC),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    base.update(kw)
    return SimpleNamespace(_mapping=base)


def _client(conn: FakeConn, role: str = "checker", perms: list[str] | None = None):
    async def claims() -> dict:
        return {"sub": "u1", "institution_id": "i1", "user_role": role,
                "module_permissions": perms if perms is not None else ["inst:bid_approval"]}

    def tc():
        yield conn

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = tc
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_pending_list_excludes_rows_past_expiry() -> None:
    conn = FakeConn({"current_member_ctx": [SimpleNamespace(id=ME)]})
    r = _client(conn).get("/approvals/pending?scope=bids")
    assert r.status_code == 200, r.text
    sql = next(q for q, _ in conn.sql if "FROM governance.action" in q)
    assert "expires_at > now()" in sql and "status = 'pending'" in sql


def test_pending_marks_own_items() -> None:
    conn = FakeConn({
        "current_member_ctx": [SimpleNamespace(id=ME)],
        "FROM governance.action": [_row(id="mine", maker_id=ME), _row(id="theirs", maker_id=OTHER)],
    })
    body = _client(conn).get("/approvals/pending?scope=bids").json()
    assert {b["id"]: b["is_mine"] for b in body} == {"mine": True, "theirs": False}


def test_queue_rejects_unknown_state_and_scope() -> None:
    c = _client(FakeConn({}))
    assert c.get("/approvals/queue?state=active").status_code == 422
    assert c.get("/approvals/queue?state=pending&scope=nope").status_code == 422


def test_queue_derives_expired_in_sql_and_reports_effective_status() -> None:
    conn = FakeConn({
        "count(*) AS n": [3],
        "current_member_ctx": [SimpleNamespace(id=ME)],
        "ORDER BY": [_row(id="x", action_status="expired")],
    })
    r = _client(conn).get("/approvals/queue?state=expired&scope=bids&limit=10")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3 and r.json()["items"][0]["action_status"] == "expired"
    listing = next(q for q, _ in conn.sql if "ORDER BY" in q)
    assert "expires_at <= now()" in listing and "THEN 'expired'" in listing
    assert next(p for q, p in conn.sql if "ORDER BY" in q)["state"] == "expired"


def test_queue_pending_sorts_by_soonest_expiry_finished_by_newest_decision() -> None:
    for state, expect in (("pending", "expires_at ASC"), ("approved", "COALESCE(checked_at")):
        conn = FakeConn({"count(*) AS n": [0], "current_member_ctx": [SimpleNamespace(id=ME)]})
        _client(conn).get(f"/approvals/queue?state={state}")
        assert expect in next(q for q, _ in conn.sql if "ORDER BY" in q)


def test_queue_is_empty_without_any_permitted_category() -> None:
    conn = FakeConn({})
    r = _client(conn, role="viewer", perms=[]).get("/approvals/queue?state=pending")
    assert r.json() == {"items": [], "total": 0}
    assert conn.sql == []  # nothing queried, nothing leaked


def test_counts_split_needs_me_from_pending() -> None:
    conn = FakeConn({
        "current_member_ctx": [SimpleNamespace(id=ME)],
        "GROUP BY": [
            SimpleNamespace(state="pending", n=3, not_mine=1),
            SimpleNamespace(state="expired", n=4, not_mine=4),
            SimpleNamespace(state="approved", n=9, not_mine=9),
        ],
    })
    assert _client(conn).get("/approvals/queue/counts?scope=bids").json() == {
        "pending": 3, "expired": 4, "approved": 9, "rejected": 0, "needs_me": 1}
