"""
E-sign email must fail closed in production.

Before: with no RESEND_API_KEY the full email (signing link, one-time code) was
written to the production log and the call reported success; a refused send was
logged and also reported success. Now: production refuses (EmailNotSent -> 502),
nothing secret is logged, and caller-supplied names/titles are HTML-escaped.
No database required.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import esign  # noqa: E402

SECRET_BODY = "<p>Your one-time signing code is <strong>482913</strong></p>"


class _Log:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _rec(self, level: str):
        return lambda event, **kw: self.calls.append((level, event, kw))

    def __getattr__(self, level: str):
        return self._rec(level)


@pytest.fixture()
def log(monkeypatch: pytest.MonkeyPatch) -> _Log:
    fake = _Log()
    monkeypatch.setattr(esign, "log", fake)
    return fake


async def test_production_without_provider_refuses_and_logs_no_secret(monkeypatch, log) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(esign.settings, "env", "production")
    with pytest.raises(esign.EmailNotSent):
        await esign._send_email("borrower@example.com", "Your Ficium signing code", SECRET_BODY)
    logged = repr(log.calls)
    assert "482913" not in logged and "signing code" not in logged
    assert "borrower@example.com" not in logged  # only the domain


async def test_dev_without_provider_logs_instead_of_sending(monkeypatch, log) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(esign.settings, "env", "development")
    await esign._send_email("dev@example.com", "s", SECRET_BODY)  # no raise
    assert log.calls and log.calls[0][1] == "esign_email_dev_mode"


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code, self.text = code, "domain not verified"


def _client(code: int | None):
    class C:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, *a: Any, **k: Any) -> _Resp:
            if code is None:
                raise esign.httpx.ConnectError("down")
            return _Resp(code)

    return C


@pytest.mark.parametrize("code", [403, 422, 500, None])
async def test_a_refused_or_unreachable_send_is_an_error(monkeypatch, log, code) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setattr(esign.httpx, "AsyncClient", _client(code))
    with pytest.raises(esign.EmailNotSent):
        await esign._send_email("b@example.com", "s", SECRET_BODY)
    assert "482913" not in repr(log.calls)


async def test_accepted_send_returns(monkeypatch, log) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setattr(esign.httpx, "AsyncClient", _client(200))
    await esign._send_email("b@example.com", "s", SECRET_BODY)


def test_caller_text_is_escaped_and_failure_is_502() -> None:
    assert esign._e("<a href='x'>Bob</a>") == "&lt;a href=&#x27;x&#x27;&gt;Bob&lt;/a&gt;"
    e = esign._email_failed("Your signing code")
    assert e.status_code == 502 and "nothing was sent" in e.detail
