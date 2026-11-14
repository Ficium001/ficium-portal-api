"""A signing link must not allow unlimited one-time codes: each new code resets the per-code
attempts, so codes are capped per signer per hour (and per IP by the rate limiter)."""
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
os.environ.setdefault("ESIGN_OTP_PEPPER", "test-pepper-0123456789abcdef")

from app.api import esign  # noqa: E402
from app.core.ratelimit import limiter  # noqa: E402
from app.main import app  # noqa: E402

SIGNER = SimpleNamespace(id=uuid.uuid4(), envelope_id=uuid.uuid4(), status="pending",
                         envelope_status="sent", email="b@example.com", display_name="B")


class _R:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class Conn:
    def __init__(self, sent_last_hour: int) -> None:
        self.sent, self.updates = sent_last_hour, 0

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _R:
        q = " ".join(str(stmt).split())
        if "event = 'otp_sent'" in q:
            return _R(SimpleNamespace(n=self.sent))
        if q.startswith("UPDATE institution.esign_signer"):
            self.updates += 1
        return _R()

    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(esign, "_live_signer", lambda conn, token: SIGNER)
    sent: list[str] = []

    async def fake_send(to: str, subject: str, html: str) -> None:
        sent.append(to)

    monkeypatch.setattr(esign, "_send_email", fake_send)
    limiter.reset()
    yield sent
    app.dependency_overrides.clear()


def _post(conn: Conn) -> Any:
    def dep():  # noqa: ANN202
        yield conn
    app.dependency_overrides[esign.service_conn] = dep
    return TestClient(app, raise_server_exceptions=False).post("/esign/public/tok/otp")


def test_code_is_sent_under_the_cap(_setup: list[str]) -> None:
    c = Conn(sent_last_hour=esign.OTP_MAX_SENDS_PER_HOUR - 1)
    r = _post(c)
    assert r.status_code == 200, r.text
    assert c.updates == 1 and _setup == ["b@example.com"]


def test_no_new_code_once_the_hourly_cap_is_reached(_setup: list[str]) -> None:
    c = Conn(sent_last_hour=esign.OTP_MAX_SENDS_PER_HOUR)
    r = _post(c)
    assert r.status_code == 429
    assert c.updates == 0 and _setup == []
