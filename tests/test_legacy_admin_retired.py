"""
The old platform admin (/admin/*) is retired by default (LEGACY_ADMIN_ENABLED off).

Its routes were single-actor (one person could grant a compliance waiver or review a
bank's documents) and did not require MFA, so they bypassed the Ficium Portal Admin
site's four-eyes controls. Off means: every route answers 410 before any token or
database work, and /admin/me says nobody is an admin. No database required.
"""

from __future__ import annotations

import os

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api.admin import router as legacy_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.deps import current_claims  # noqa: E402
from app.main import app  # noqa: E402

STAFF = {"sub": "00000000-0000-0000-0000-0000000000aa", "role": "authenticated"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    assert settings.legacy_admin_enabled is False, "the default must stay OFF"

    async def claims() -> dict:
        return STAFF

    app.dependency_overrides[current_claims] = claims
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(current_claims, None)


def _admin_routes() -> list[tuple[str, str]]:
    out = []
    for r in legacy_router.routes:  # every route of the old (guarded) admin router
        if isinstance(r, APIRoute):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                out.append((m, r.path))
    return sorted(out)


def test_every_legacy_admin_route_is_gone(client: TestClient) -> None:
    routes = _admin_routes()
    assert len(routes) >= 15, routes  # the whole old surface, not a sample
    for method, path in routes:
        url = path.replace("{iid}", "00000000-0000-0000-0000-000000000001").replace(
            "{doc_id}", "00000000-0000-0000-0000-000000000002"
        )
        r = client.request(method, url, json={})
        assert r.status_code == 410, f"{method} {path} answered {r.status_code}"
        assert "Portal Admin site" in r.json()["detail"]


def test_the_single_actor_waiver_grant_is_refused_without_a_token(client: TestClient) -> None:
    app.dependency_overrides.pop(current_claims, None)
    r = client.put(
        "/admin/institutions/00000000-0000-0000-0000-000000000001/compliance-waiver",
        json={"reason": "x" * 20, "waived_until": "2099-01-01T00:00:00Z"},
    )
    assert r.status_code == 410  # refused before authentication or any database call


def test_admin_me_says_nobody_is_an_admin(client: TestClient) -> None:
    r = client.get("/admin/me")
    assert r.status_code == 200 and r.json() is None
