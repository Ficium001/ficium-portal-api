"""POST /auth/register: one server-side transaction creates the institution
(unapproved), the ficium-auth login and the primary-admin member, or nothing.
No Supabase Auth, no browser writes to the database."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.ratelimit import limiter
from app.main import app

GOOD = {
    "institution_name": "ZZ Bank", "institution_type": "commercial_bank", "country": "mu",
    "first_name": "Ada", "last_name": "Lovelace", "email": " Ada@ZZBank.mu ",
    "password": "Str0ng-enough-pw",
}


class Svc:
    def __init__(self, taken: bool = False) -> None:
        self.taken = taken
        self.sql: list[tuple[str, dict[str, Any]]] = []

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        q = " ".join(str(stmt).split())
        self.sql.append((q, params or {}))
        row = None
        if "lower(email) = :e" in q:
            row = (1,) if self.taken else None
        elif q.startswith("INSERT INTO institution.institution"):
            row = SimpleNamespace(id=uuid.uuid4())
        return SimpleNamespace(fetchone=lambda: row)

    def inserts(self) -> list[str]:
        return [q.split(" (")[0].removeprefix("INSERT INTO ") for q, _ in self.sql
                if q.startswith("INSERT INTO")]


@pytest.fixture(autouse=True)
def _reset_limits() -> Any:
    limiter.reset()
    yield
    limiter.reset()


def _post(svc: Svc, body: dict[str, Any]) -> Any:
    @contextmanager
    def _session():  # noqa: ANN202
        yield svc

    with patch("app.api.registration.service_session", _session):
        return TestClient(app, raise_server_exceptions=False).post("/auth/register", json=body)


def test_registers_institution_login_and_admin_in_one_session() -> None:
    svc = Svc()
    r = _post(svc, GOOD)
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "ada@zzbank.mu"
    assert svc.inserts() == ["institution.institution", "auth_portal.auth_users",
                             "institution.member", "audit.event"]
    inst_q = next(q for q, _ in svc.sql if q.startswith("INSERT INTO institution.institution"))
    assert "false, :email" in inst_q and "'registered'" in inst_q  # born unapproved
    login = next(p for q, p in svc.sql if q.startswith("INSERT INTO auth_portal.auth_users"))
    member = next(p for q, p in svc.sql if q.startswith("INSERT INTO institution.member"))
    assert login["id"] == member["uid"]                           # one identity
    login_q = next(q for q, _ in svc.sql if q.startswith("INSERT INTO auth_portal.auth_users"))
    assert ":email, :email" in login_q                            # username = email
    assert login["pw"].startswith("$argon2id$") and "Str0ng" not in login["pw"]
    assert not any("auth.users" in q.replace("auth_portal.auth_users", "") for q, _ in svc.sql)


@pytest.mark.parametrize("pw", ["short1A!", "alllowercaseletters", "ALLUPPER123456"])
def test_weak_passwords_rejected_before_any_write(pw: str) -> None:
    svc = Svc()
    assert _post(svc, {**GOOD, "password": pw}).status_code == 422
    assert svc.sql == []


@pytest.mark.parametrize("field,value", [("institution_type", "bank"), ("email", "not-an-email"),
                                         ("country", "MAURITIUS")])
def test_invalid_fields_rejected(field: str, value: str) -> None:
    svc = Svc()
    assert _post(svc, {**GOOD, field: value}).status_code == 422
    assert svc.sql == []


def test_existing_email_gets_a_generic_refusal_and_no_writes() -> None:
    svc = Svc(taken=True)
    r = _post(svc, GOOD)
    assert r.status_code == 409
    assert "already" not in r.json()["detail"].split(".")[0]  # no "this email exists"
    assert svc.inserts() == []


def test_rate_limited_per_ip() -> None:
    codes = [_post(Svc(), {**GOOD, "email": f"u{i}@zz.mu"}).status_code for i in range(6)]
    assert codes[:5] == [201] * 5 and codes[5] == 429
