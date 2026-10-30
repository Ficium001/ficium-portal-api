"""Per-institution compliance waiver (db/051): Ficium admins only, reason
required, expiry in the future and at most 180 days, audited."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.deps import current_claims
from app.main import app

IID = str(uuid.uuid4())
ROW = SimpleNamespace(_mapping={
    "is_approved": True, "is_compliant": False, "missing_docs": ["aml_policy"], "can_bid": True,
    "waived_until": None, "reason": "pilot", "granted_at": None, "granted_by": None,
    "revoked_at": None})


class Svc:
    def __init__(self, admin: bool = True, active: bool = True) -> None:
        self.admin, self.active = admin, active
        self.sql: list[str] = []

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        q = " ".join(str(stmt).split())
        self.sql.append(q)
        if "portal_admin.admin_users" in q:
            r = SimpleNamespace(id="a1") if self.admin else None
        elif q.startswith("UPDATE institution.compliance_waiver"):
            r = (IID,) if self.active else None
        elif "FROM institution.compliance c" in q:
            r = ROW
        else:
            r = None
        return SimpleNamespace(fetchone=lambda: r)


def _call(svc: Svc, method: str, body: dict | None = None) -> Any:
    async def claims() -> dict[str, Any]:
        return {"sub": str(uuid.uuid4())}

    @contextmanager
    def session():  # noqa: ANN202
        yield svc

    app.dependency_overrides[current_claims] = claims
    try:
        with patch("app.api.admin.service_session", session):
            c = TestClient(app, raise_server_exceptions=False)
            url = f"/admin/institutions/{IID}/compliance-waiver"
            return c.put(url, json=body) if method == "put" else c.delete(url)
    finally:
        app.dependency_overrides.clear()


def _until(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def test_admin_grants_a_waiver_and_it_is_audited() -> None:
    svc = Svc()
    body = {"reason": "MCB pilot, documents in progress", "waived_until": _until(30)}
    r = _call(svc, "put", body)
    assert r.status_code == 200, r.text
    assert any(q.startswith("INSERT INTO institution.compliance_waiver") for q in svc.sql)
    assert any(q.startswith("INSERT INTO audit.event") for q in svc.sql)


@pytest.mark.parametrize("body", [
    {"reason": "too short", "waived_until": _until(30)},
    {"reason": "a perfectly fine reason", "waived_until": _until(-1)},
    {"reason": "a perfectly fine reason", "waived_until": _until(181)},
])
def test_invalid_waivers_are_refused_without_writes(body: dict) -> None:
    svc = Svc()
    assert _call(svc, "put", body).status_code == 422
    assert not any(q.startswith("INSERT") for q in svc.sql)


def test_non_admin_cannot_grant() -> None:
    svc = Svc(admin=False)
    r = _call(svc, "put", {"reason": "a perfectly fine reason", "waived_until": _until(30)})
    assert r.status_code == 403
    assert not any(q.startswith("INSERT") for q in svc.sql)


def test_revoke_active_waiver_and_404_when_none() -> None:
    assert _call(Svc(active=True), "delete").status_code == 200
    assert _call(Svc(active=False), "delete").status_code == 404


@pytest.fixture(autouse=True)
def _legacy_admin_on(monkeypatch):
    """These tests cover the retired /admin/* routes, which are off by default."""
    from app.core.config import settings as _s

    monkeypatch.setattr(_s, "legacy_admin_enabled", True)
