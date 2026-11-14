"""db/063: approving a request whose record changed since it was made is refused, and the
approver is told why in plain words (409), not shown a raw database error."""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import approvals  # noqa: E402
from app.deps import current_claims as get_claims  # noqa: E402
from app.deps import tenant_conn
from app.main import app  # noqa: E402


class Conn:
    def __init__(self, err: str | None) -> None:
        self.err = err

    def execute(self, stmt: Any, params: dict | None = None) -> Any:
        if "approve_action" in str(stmt) and self.err:
            raise DBAPIError(str(stmt), params, Exception(self.err))
        return type("R", (), {"fetchone": lambda s: type("Row", (), {"res": None})()})()


def _post(conn: Conn, monkeypatch: Any) -> Any:
    monkeypatch.setattr(approvals, "_check_action_permission", lambda *a, **k: None)

    async def claims() -> dict:
        return {"sub": str(uuid.uuid4()), "institution_id": str(uuid.uuid4())}

    def tenant():  # noqa: ANN202
        yield conn

    app.dependency_overrides[get_claims] = claims
    app.dependency_overrides[tenant_conn] = tenant
    try:
        url = f"/approvals/{uuid.uuid4()}/approve"
        return TestClient(app, raise_server_exceptions=False).post(url, json={})
    finally:
        app.dependency_overrides.clear()


def test_changed_record_is_a_plain_409(monkeypatch: Any) -> None:
    err = "CHANGED_SINCE_REQUEST: this record changed after the request was made"
    r = _post(Conn(err), monkeypatch)
    assert r.status_code == 409
    assert "changed after the request was made" in r.json()["detail"]
    assert "CHANGED_SINCE_REQUEST" not in r.json()["detail"]


def test_other_refusals_stay_400(monkeypatch: Any) -> None:
    own = Conn("Maker cannot approve own action (4-eyes control)")
    assert _post(own, monkeypatch).status_code == 400


def test_clean_approval_is_200(monkeypatch: Any) -> None:
    assert _post(Conn(None), monkeypatch).status_code == 200
