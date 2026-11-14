"""The portal has no self-registration (KJ 2026-11-13): institutions apply via /access-requests
and two admins provision them. The old endpoints must not come back, even behind a switch."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402


@pytest.mark.parametrize("path", ["/auth/register", "/auth/provision-member"])
def test_self_registration_endpoints_do_not_exist(path: str) -> None:
    r = TestClient(app, raise_server_exceptions=False).post(path, json={})
    assert r.status_code in (404, 405)


def test_there_is_no_switch_to_turn_it_back_on() -> None:
    assert not hasattr(settings, "self_registration_enabled")
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/auth/register" not in paths and "/auth/provision-member" not in paths
