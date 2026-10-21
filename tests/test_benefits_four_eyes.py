"""Every benefit change goes through governance.submit, never a direct write
(db/040, 042). Until 2026-10-19 only "guaranteed" benefits had four-eyes."""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

INST = str(uuid.uuid4())
CAT = str(uuid.uuid4())
BEN = str(uuid.uuid4())


class _R:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row

    def scalar(self) -> Any:
        return getattr(self._row, "code", None)


class Conn:
    def __init__(self, control: str) -> None:
        self.sql: list[tuple[str, dict[str, Any]]] = []
        self.control = control

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _R:
        q = " ".join(str(stmt).split())
        self.sql.append((q, params or {}))
        if "FROM institution.benefit_cat" in q:
            return _R(SimpleNamespace(code="reward"))
        if "submit_for_approval" in q:
            status = "approved" if self.control == "single" else "pending"
            return _R(SimpleNamespace(id=uuid.uuid4(), status=status, control=self.control))
        if "FROM institution.benefit" in q:
            existing = SimpleNamespace(
                institution_id=INST, is_guaranteed=False,
                _mapping={"id": BEN, "institution_id": INST, "cat_id": CAT, "title": "Old",
                          "is_guaranteed": False, "is_active": True,
                          "value_amount": 500, "value_unit": "mur"},
            )
            return _R(existing)
        return _R()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def writes(self) -> list[str]:
        return [q for q, _ in self.sql
                if q.startswith(("INSERT INTO institution.benefit", "UPDATE institution.benefit"))]


def _client(conn: Conn) -> TestClient:
    async def claims() -> dict[str, Any]:
        return {"sub": str(uuid.uuid4()), "institution_id": INST, "user_role": "maker",
                "module_permissions": ["inst:benefits"]}

    def tenant(claims: Any = None):  # noqa: ANN202
        yield conn

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = tenant
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    yield
    app.dependency_overrides.clear()


BODY = {"cat_id": CAT, "title": "Cashback", "value_amount": 500, "value_unit": "mur",
        "is_guaranteed": False}


@pytest.mark.parametrize("control,pending", [("four_eyes", True), ("single", False)])
def test_ordinary_benefit_goes_through_the_switch(control: str, pending: bool) -> None:
    c = Conn(control)
    r = _client(c).post("/benefits", json=BODY)
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is pending
    assert c.writes() == []
    assert any(p.get("cat") == "benefit.create" for _, p in c.sql)


def test_update_and_deactivate_go_through_the_switch() -> None:
    c = Conn("four_eyes")
    client = _client(c)
    assert client.put(f"/benefits/{BEN}", json={"title": "New title"}).status_code == 200
    assert client.delete(f"/benefits/{BEN}").status_code == 200
    cats = [p.get("cat") for _, p in c.sql if "submit_for_approval" in _]
    assert cats == ["benefit.update", "benefit.delete"]
    assert c.writes() == []
