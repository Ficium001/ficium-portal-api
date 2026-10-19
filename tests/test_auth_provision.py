"""provision-member may only create the first login of a caller's own,
still-unapproved institution — never a login inside an existing bank, and
never with a caller-chosen role (critical fix, 2026-10-19)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

UID = "11111111-1111-1111-1111-111111111111"
IID = "22222222-2222-2222-2222-222222222222"
EMAIL = "a@bank.mu"


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class FakeConn:
    def __init__(self, *, auth_user: bool = True, own_registration: bool = True,
                 other_login: bool = False, email_taken: bool = False) -> None:
        self.cfg = dict(
            auth_user=auth_user, own=own_registration, other=other_login, taken=email_taken,
        )
        self.inserted: list[dict[str, Any]] = []

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(stmt).split())
        p = params or {}
        if "FROM auth.users" in sql:
            return _Result((UID,) if self.cfg["auth_user"] else None)
        if "JOIN institution.member" in sql:
            # The query must bind the caller's identity, not just the institution.
            assert p["uid"] == UID and p["email"] == EMAIL and p["iid"] == IID
            assert "i.approved = false" in sql and "onboarding_stage = 'registered'" in sql
            return _Result((IID,) if self.cfg["own"] else None)
        if "WHERE institution_id = :iid" in sql:
            return _Result((1,) if self.cfg["other"] else None)
        if "WHERE email = :email" in sql:
            return _Result((UID,) if self.cfg["taken"] else None)
        if sql.startswith("INSERT INTO auth_portal.auth_users"):
            self.inserted.append(p)
            return _Result(None)
        raise AssertionError(f"unexpected SQL: {sql}")


def _post(conn: FakeConn, **body: Any):
    @contextmanager
    def service():
        yield conn

    payload = {
        "auth_user_id": UID, "email": EMAIL, "password": "longenough1",
        "institution_id": IID, **body,
    }
    with patch("app.api.auth_provision.service_session", service):
        client = TestClient(app, raise_server_exceptions=False)
        return client.post("/auth/provision-member", json=payload)


def test_own_new_institution_gets_first_login_as_institution_admin() -> None:
    conn = FakeConn()
    r = _post(conn)
    assert r.status_code == 200 and r.json()["created"] is True
    assert conn.inserted[0]["role"] == "institution_admin"


def test_role_in_request_is_ignored() -> None:
    conn = FakeConn()
    assert _post(conn, role="super_admin").status_code == 200
    assert conn.inserted[0]["role"] == "institution_admin"


def test_someone_elses_or_approved_institution_is_refused() -> None:
    conn = FakeConn(own_registration=False)
    assert _post(conn).status_code == 403
    assert conn.inserted == []


def test_second_login_for_an_institution_is_refused() -> None:
    conn = FakeConn(other_login=True)
    assert _post(conn).status_code == 403
    assert conn.inserted == []


def test_unknown_auth_user_is_refused() -> None:
    conn = FakeConn(auth_user=False)
    assert _post(conn).status_code == 400
    assert conn.inserted == []
