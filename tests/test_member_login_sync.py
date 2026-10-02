"""Member login state: tenant sessions must never touch auth_portal.

Background: auth_portal has row-level security with no policies, so a tenant session
(role `authenticated`) can neither read nor write auth_users. The login row is kept in step with
institution.member by the DB trigger `institution.sync_member_login_state` (active, email), and the
one flow that must CREATE a login (approve-user provisioning) runs in a single privileged session.
These tests record the SQL each endpoint sends and to which session.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

INST = str(uuid.uuid4())
OTHER_INST = str(uuid.uuid4())
MEMBER = str(uuid.uuid4())
AUTH_UID = str(uuid.uuid4())
ACTION = str(uuid.uuid4())
NEW_UID = str(uuid.uuid4())


class _Result:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return [] if self._row is None else [self._row]


class Recorder:
    """Stands in for a SQLAlchemy Session; records (sql, params) and replays scripted rows."""

    def __init__(
        self,
        script: list[tuple[str, Any]] | None = None,
        raise_on: tuple[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self._script = script or []
        self._raise_on = raise_on

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(stmt).split())
        self.calls.append((sql, params or {}))
        if self._raise_on and self._raise_on[0] in sql:
            raise self._raise_on[1]
        for needle, row in self._script:
            if needle in sql:
                return _Result(row)
        return _Result(None)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass

    def sqls(self) -> list[str]:
        return [c[0] for c in self.calls]


def _claims() -> dict[str, Any]:
    return {
        "sub": str(uuid.uuid4()),
        "role": "authenticated",
        "user_role": "institution_admin",
        "institution_id": INST,
    }


def _install(tenant: Recorder) -> TestClient:
    async def _claims_dep() -> dict[str, Any]:
        return _claims()

    def _tenant_dep(claims: Any = None):  # noqa: ANN202
        yield tenant

    app.dependency_overrides[current_claims] = _claims_dep
    app.dependency_overrides[tenant_conn] = _tenant_dep
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    yield
    app.dependency_overrides.clear()


def _member_row() -> SimpleNamespace:
    return SimpleNamespace(id=MEMBER, is_primary_admin=False, auth_user_id=AUTH_UID)


# ── deactivate / reactivate / email change ──────────────────────────────────
def test_deactivate_updates_only_the_member_row() -> None:
    t = Recorder(script=[("FROM institution.member WHERE id", _member_row())])
    r = _install(t).post(f"/members/{MEMBER}/deactivate")
    assert r.status_code == 200, r.text
    assert not any("auth_portal" in s for s in t.sqls()), t.sqls()
    assert any("SET active = false" in s for s in t.sqls())
    assert t.commits == 1


def test_reactivate_updates_only_the_member_row() -> None:
    t = Recorder(script=[("FROM institution.member WHERE id", _member_row())])
    r = _install(t).post(f"/members/{MEMBER}/reactivate")
    assert r.status_code == 200, r.text
    assert not any("auth_portal" in s for s in t.sqls()), t.sqls()
    assert any("SET active = true" in s for s in t.sqls())


def test_email_change_updates_member_only_and_lowercases() -> None:
    t = Recorder(script=[("FROM institution.member WHERE id", _member_row())])
    r = _install(t).patch(f"/members/{MEMBER}", json={"email": "  New.Person@Example.com "})
    assert r.status_code == 200, r.text
    assert not any("auth_portal" in s for s in t.sqls()), t.sqls()
    update = next(c for c in t.calls if c[0].startswith("UPDATE institution.member"))
    assert update[1]["email"] == "new.person@example.com"


def test_email_already_used_by_another_login_is_a_409() -> None:
    t = Recorder(
        script=[("FROM institution.member WHERE id", _member_row())],
        raise_on=(
            "UPDATE institution.member SET",
            IntegrityError("stmt", {}, Exception("duplicate key uq_auth_users_email")),
        ),
    )
    r = _install(t).patch(f"/members/{MEMBER}", json={"email": "taken@example.com"})
    assert r.status_code == 409
    assert t.rollbacks == 1 and t.commits == 0


def test_approved_email_update_touches_only_member_and_conflicts_cleanly() -> None:
    action = SimpleNamespace(
        id=ACTION,
        category="user.update",
        status="approved",
        payload={"member_id": MEMBER, "field": "email", "value": "x@example.com"},
    )
    t = Recorder(script=[("FROM governance.action WHERE id", action)])
    r = _install(t).post(f"/approvals/{ACTION}/execute-update")
    assert r.status_code == 200, r.text
    assert not any("auth_portal" in s for s in t.sqls()), t.sqls()

    t2 = Recorder(
        script=[("FROM governance.action WHERE id", action)],
        raise_on=("UPDATE institution.member SET email", IntegrityError("s", {}, Exception("dup"))),
    )
    assert _install(t2).post(f"/approvals/{ACTION}/execute-update").status_code == 409
    assert t2.rollbacks == 1


# ── provisioning a user from an approved action ─────────────────────────────
def _action(**payload: Any) -> SimpleNamespace:
    base = {
        "email": "New.User@Example.com",
        "username": "New.User",
        "first_name": "New",
        "last_name": "User",
        "custom_group_id": str(uuid.uuid4()),
        "member_role": "maker",
    }
    return SimpleNamespace(
        id=ACTION,
        category="user.create",
        status="approved",
        institution_id=INST,
        payload={**base, **payload},
    )


def _provision(tenant: Recorder, svc: Recorder) -> Any:
    @contextmanager
    def _svc():  # noqa: ANN202
        yield svc

    client = _install(tenant)
    with patch("app.api.approvals.service_session", _svc):
        return client.post(f"/approvals/{ACTION}/provision-user")


def test_provision_creates_login_member_and_marks_action_in_one_privileged_session() -> None:
    tenant = Recorder(script=[("FROM governance.action a", _action())])
    svc = Recorder(script=[("INSERT INTO auth_portal.auth_users", SimpleNamespace(id=NEW_UID))])
    r = _provision(tenant, svc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["created"] is True and body["user_id"] == NEW_UID and len(body["temp_password"]) == 16
    )
    assert body["email"] == "new.user@example.com" and body["username"] == "new.user"
    # the tenant session only READ the action; nothing of auth_portal went through it
    assert len(tenant.calls) == 1 and "auth_portal" not in tenant.sqls()[0]
    # everything else, in one privileged session, in order
    order = [s.split("(")[0].split(" WHERE")[0].strip() for s in svc.sqls()]
    assert any("INSERT INTO auth_portal.auth_users" in s for s in svc.sqls())
    assert any("INSERT INTO institution.member" in s for s in svc.sqls())
    assert any("UPDATE governance.action" in s for s in svc.sqls())
    assert [i for i, s in enumerate(order) if "auth_users" in s and "INSERT" in s][0] < [
        i for i, s in enumerate(order) if "institution.member" in s and "INSERT" in s
    ][0]


def test_provision_never_links_a_login_that_belongs_to_another_institution() -> None:
    tenant = Recorder(script=[("FROM governance.action a", _action())])
    svc = Recorder(
        script=[
            (
                "SELECT id, institution_id FROM auth_portal.auth_users",
                SimpleNamespace(id=NEW_UID, institution_id=OTHER_INST),
            )
        ]
    )
    r = _provision(tenant, svc)
    assert r.status_code == 409
    assert not any(s.startswith("INSERT") or s.startswith("UPDATE") for s in svc.sqls()), svc.sqls()


def test_provision_is_idempotent_inside_the_same_institution() -> None:
    existing = SimpleNamespace(id=NEW_UID, institution_id=INST)
    # login and member both exist -> no writes at all
    svc = Recorder(
        script=[
            ("SELECT id, institution_id FROM auth_portal.auth_users", existing),
            ("SELECT id FROM institution.member", SimpleNamespace(id=MEMBER)),
        ]
    )
    r = _provision(Recorder(script=[("FROM governance.action a", _action())]), svc)
    assert r.status_code == 200 and r.json()["created"] is False
    assert not any(s.startswith("INSERT") for s in svc.sqls())
    # login exists but the member row is missing -> only the member is created
    svc2 = Recorder(script=[("SELECT id, institution_id FROM auth_portal.auth_users", existing)])
    r2 = _provision(Recorder(script=[("FROM governance.action a", _action())]), svc2)
    assert r2.status_code == 200 and r2.json()["created"] is False
    inserts = [s for s in svc2.sqls() if s.startswith("INSERT")]
    assert len(inserts) == 1 and "institution.member" in inserts[0]


def test_provision_duplicate_username_or_email_is_a_409() -> None:
    svc = Recorder(
        raise_on=("INSERT INTO auth_portal.auth_users", IntegrityError("s", {}, Exception("dup")))
    )
    r = _provision(Recorder(script=[("FROM governance.action a", _action())]), svc)
    assert r.status_code == 409


@pytest.mark.parametrize(
    ("category", "status", "code"),
    [("user.update", "approved", 400), ("user.create", "pending", 400)],
)
def test_provision_rejects_wrong_action_state_before_any_privileged_work(
    category: str, status: str, code: int
) -> None:
    row = SimpleNamespace(
        id=ACTION, category=category, status=status, institution_id=INST, payload={}
    )
    svc = Recorder()
    r = _provision(Recorder(script=[("FROM governance.action a", row)]), svc)
    assert r.status_code == code
    assert svc.calls == []
