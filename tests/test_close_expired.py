"""Closing expired bid windows: one runner at a time, correct counts, no false bank notifications,
and a scheduler that is OFF unless an operator turns it on."""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import marketplace  # noqa: E402


class _Res:
    def __init__(self, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar, self._rows = scalar, rows or []

    def scalar(self) -> Any:
        return self._scalar

    def fetchall(self) -> list[Any]:
        return self._rows


class _Sess:
    def __init__(self, lock: bool, rows: list[Any]) -> None:
        self.lock, self.rows, self.sql = lock, rows, []

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Res:
        s = " ".join(str(stmt).split())
        self.sql.append(s)
        if "pg_try_advisory_xact_lock" in s:
            return _Res(scalar=self.lock)
        return _Res(rows=self.rows)


def _patch_session(monkeypatch: pytest.MonkeyPatch, sess: _Sess) -> None:
    @contextmanager
    def _svc():  # noqa: ANN202
        yield sess

    monkeypatch.setattr(marketplace, "service_session", _svc)


def _rows(*statuses: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(request_id=f"r{i}", new_status=s) for i, s in enumerate(statuses)]


def test_counts_closed_and_expired_from_the_set_returning_function(monkeypatch) -> None:
    sess = _Sess(lock=True, rows=_rows("closed", "expired", "expired"))
    _patch_session(monkeypatch, sess)
    assert marketplace.close_expired_once() == {"closed": 1, "expired": 2, "skipped": 0}
    assert any("FROM marketplace.close_expired_windows()" in s for s in sess.sql)


def test_second_runner_backs_off_without_touching_anything(monkeypatch) -> None:
    sess = _Sess(lock=False, rows=_rows("expired"))
    _patch_session(monkeypatch, sess)
    assert marketplace.close_expired_once() == {"closed": 0, "expired": 0, "skipped": 1}
    assert not any("close_expired_windows" in s for s in sess.sql)


def test_scheduler_is_off_by_default() -> None:
    from app.core.config import Settings

    assert Settings().maintenance_close_expired_enabled is False
