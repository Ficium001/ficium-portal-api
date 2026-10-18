"""A decline is per institution and never touches the borrower database (db/035, contract 1.8.0)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import marketplace as mp  # noqa: E402
from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

RID = "00000000-0000-4000-8000-000000000001"
INST = "00000000-0000-4000-8000-000000000060"


class _R:
    def __init__(self, row: Any = None) -> None:
        self.row = row

    def fetchone(self) -> Any:
        return self.row


class Tenant:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    def execute(self, *_a: Any, **_k: Any) -> _R:
        return _R(object() if self.visible else None)


class Svc:
    def __init__(self, error: str | None) -> None:
        self.error, self.sql = error, []

    def execute(self, stmt: Any, params: Any = None) -> _R:
        q = str(stmt)
        self.sql.append(q)
        if "decline_request" in q and self.error:
            raise DBAPIError("SELECT", {}, Exception(f"ERROR: {self.error}"))
        return _R()


@contextmanager
def client(visible: bool = True, error: str | None = None):
    svc = Svc(error)

    async def claims() -> dict:
        return {"sub": "u1", "institution_id": INST, "user_role": "checker", "email": "a@b.mu"}

    def tc():
        yield Tenant(visible)

    @contextmanager
    def service():
        yield svc

    def no_app_db():
        raise AssertionError("a decline must never open a borrower-database session")

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = tc
    try:
        with patch.object(mp, "service_session", service), \
             patch("app.core.db.app_service_session", no_app_db), \
             patch.object(mp, "dispatch_event", lambda *a, **k: _noop()):
            yield TestClient(app, raise_server_exceptions=False), svc
    finally:
        app.dependency_overrides.clear()


async def _noop() -> None:
    return None


def test_decline_records_per_institution_and_audits():
    with client() as (c, svc):
        r = c.post(f"/marketplace/requests/{RID}/reject", json={"reason": "Outside our criteria."})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "declined"
    assert any("marketplace.decline_request" in q for q in svc.sql)
    assert any("'request.decline'" in q for q in svc.sql)
    # never cancels the request for everyone
    assert not any("UPDATE marketplace.request" in q for q in svc.sql)


def test_request_not_visible_to_this_institution():
    with client(visible=False) as (c, _):
        assert c.post(f"/marketplace/requests/{RID}/reject", json={}).status_code == 404


@pytest.mark.parametrize(("code", "status", "text_"), [
    ("DECLINE_HAS_BID", 409, "Withdraw it instead"),
    ("DECLINE_ALREADY", 409, "already declined"),
    ("DECLINE_NOT_OPEN", 409, "no longer open"),
])
def test_refusals(code, status, text_):
    with client(error=code) as (c, _):
        r = c.post(f"/marketplace/requests/{RID}/reject", json={})
    assert r.status_code == status and text_ in r.json()["detail"]


def test_marketplace_module_has_no_borrower_database_access():
    import inspect

    src = inspect.getsource(mp)
    assert "app_service_session" not in src          # no session to the borrower database
    assert "SELECT public.reject_request" not in src  # no call into it
