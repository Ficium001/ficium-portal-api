"""Setup > Controls: the four-eyes switches. Enforcement is in the database
(db/040); these tests pin the API surface: admins only, switch-off is queued
for a second person, switch-on is immediate, database refusals surface as 409."""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402


class _R:
    def __init__(self, row: Any = None, rows: list[Any] | None = None) -> None:
        self._row, self._rows = row, rows or []

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return self._rows


class Conn:
    def __init__(self, fail: str | None = None) -> None:
        self.sql: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.commits = 0

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _R:
        q = " ".join(str(stmt).split())
        self.sql.append((q, params or {}))
        if self.fail and self.fail in q:
            msg = "ERROR:  Four-eyes on user.create is locked by the platform\n"
            orig = SimpleNamespace(pgerror=msg)
            raise DBAPIError(q, {}, orig)  # type: ignore[arg-type]
        if "submit_for_approval" in q:
            return _R(SimpleNamespace(id=uuid.uuid4()))
        if "four_eyes_catalog" in q:
            row = SimpleNamespace(_mapping={
                "category": "benefit.create", "label": "Add a benefit",
                "screen": "Setup › Products",
                "locked": False, "locked_reason": None, "sort_order": 30, "enabled": True,
                "can_change": True,
                "changed_at": None, "pending_disable_action_id": None,
            })
            return _R(rows=[row])
        return _R()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def _client(conn: Conn, role: str = "institution_admin") -> TestClient:
    async def claims() -> dict[str, Any]:
        return {"sub": str(uuid.uuid4()), "user_role": role, "institution_id": str(uuid.uuid4())}

    def tenant(claims: Any = None):  # noqa: ANN202
        yield conn

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = tenant
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    yield
    app.dependency_overrides.clear()


def test_list_shows_every_switch() -> None:
    r = _client(Conn()).get("/controls/four-eyes")
    assert r.status_code == 200
    assert r.json()[0]["category"] == "benefit.create"


def test_switch_off_is_queued_for_a_second_person() -> None:
    c = Conn()
    r = _client(c).post("/controls/four-eyes/benefit.create/disable")
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    q, params = next(x for x in c.sql if "submit_for_approval" in x[0])
    assert "'four_eyes.disable'" in q and '"benefit.create"' in params["p"]


def test_switch_on_is_immediate() -> None:
    c = Conn()
    r = _client(c).post("/controls/four-eyes/benefit.create/enable")
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert any("enable_four_eyes" in q for q, _ in c.sql) and c.commits == 1


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_non_admins_cannot_change_switches(action: str) -> None:
    c = Conn()
    r = _client(c, role="maker").post(f"/controls/four-eyes/benefit.create/{action}")
    assert r.status_code == 403 and c.sql == []


def test_locked_control_refusal_surfaces_as_409() -> None:
    r = _client(Conn(fail="submit_for_approval")).post("/controls/four-eyes/user.create/disable")
    assert r.status_code == 409
    assert "locked" in r.json()["detail"]


def test_malformed_category_is_404_without_touching_the_db() -> None:
    c = Conn()
    assert _client(c).post("/controls/four-eyes/DROP TABLE/disable").status_code == 404
    assert c.sql == []
