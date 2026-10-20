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


# ── deactivate / reactivate / edit: four-eyes, never a direct write ─────────
# Since db/040 these endpoints only QUEUE a four-eyes action; the database
# applies the change when a second person approves. They must never write the
# member row or the login row themselves.
def _queued() -> SimpleNamespace:
    return SimpleNamespace(id=ACTION, status="pending", control="four_eyes")


def _no_direct_writes(t: Recorder) -> None:
    assert not any("auth_portal" in s for s in t.sqls()), t.sqls()
    assert not any(s.startswith("UPDATE institution.member") for s in t.sqls()), t.sqls()


def test_deactivate_queues_four_eyes_and_writes_nothing() -> None:
    t = Recorder(script=[("FROM institution.member WHERE id", _member_row()),
                         ("FROM governance.action", _queued())])
    r = _install(t).post(f"/members/{MEMBER}/deactivate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    _no_direct_writes(t)
    submit = next(c for c in t.calls if "submit_for_approval" in c[0])
    assert submit[1]["cat"] == "user.deactivate"


def test_reactivate_queues_four_eyes_and_writes_nothing() -> None:
    t = Recorder(script=[("FROM institution.member WHERE id", _member_row()),
                         ("FROM governance.action", _queued())])
    r = _install(t).post(f"/members/{MEMBER}/reactivate")
    assert r.status_code == 200, r.text
    _no_direct_writes(t)
    assert any(c[1].get("cat") == "user.reactivate" for c in t.calls)


def test_edit_queues_one_action_per_field_and_lowercases_email() -> None:
    t = Recorder(script=[("FROM institution.member WHERE id", _member_row()),
                         ("FROM governance.action", _queued())])
    r = _install(t).patch(
        f"/members/{MEMBER}",
        json={"email": "  New.Person@Example.com ", "member_role": "checker"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    _no_direct_writes(t)
    payloads = [c[1]["payload"] for c in t.calls if "submit_for_approval" in c[0]]
    assert len(payloads) == 2
    assert '"new.person@example.com"' in payloads[0]


def test_primary_admin_cannot_be_deactivated_or_edited() -> None:
    primary = SimpleNamespace(id=MEMBER, is_primary_admin=True, auth_user_id=AUTH_UID)
    t = Recorder(script=[("FROM institution.member WHERE id", primary)])
    client = _install(t)
    assert client.post(f"/members/{MEMBER}/deactivate").status_code == 403
    assert client.patch(f"/members/{MEMBER}", json={"full_name": "x"}).status_code == 403
    assert not any("submit_for_approval" in s for s in t.sqls())


def test_execute_update_only_reports_what_the_database_did() -> None:
    done = SimpleNamespace(category="user.update", status="approved", execution_status="executed")
    t = Recorder(script=[("FROM governance.action WHERE id", done)])
    r = _install(t).post(f"/approvals/{ACTION}/execute-update")
    assert r.status_code == 200, r.text
    _no_direct_writes(t)

    pending = SimpleNamespace(category="user.update", status="pending", execution_status="pending")
    t2 = Recorder(script=[("FROM governance.action WHERE id", pending)])
    assert _install(t2).post(f"/approvals/{ACTION}/execute-update").status_code == 409
    _no_direct_writes(t2)


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


CHECKER = str(uuid.uuid4())


def _provision(tenant: Recorder, svc: Recorder, *, claim: bool = True) -> Any:
    """The caller is the approving checker, and the credential is unclaimed,
    unless claim=False (db/043: released once, only to the checker)."""
    tenant._script.append(("current_member_ctx", SimpleNamespace(id=CHECKER)))
    if claim:
        svc._script.insert(0, ("credential_released_at = now()",
                               SimpleNamespace(payload={}, institution_id=INST)))

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
    # the tenant session only READ (the action, who I am); no auth_portal through it
    assert len(tenant.calls) == 2 and not any("auth_portal" in q for q in tenant.sqls())
    # the credential was claimed for THIS checker, in the privileged session
    claim = next(c for c in svc.calls if "credential_released_at = now()" in c[0])
    assert claim[1]["me"] == CHECKER and claim[1]["cat"] == "user.create"
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
    # Only the credential claim ran; the 409 rolls the whole service
    # transaction back (service_session), claim included.
    writes = [q for q in svc.sqls()
              if q.startswith(("INSERT", "UPDATE")) and "credential_released_at" not in q]
    assert writes == [], writes


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


def test_provision_refused_for_anyone_but_the_checker_or_a_second_time() -> None:
    tenant = Recorder(script=[("FROM governance.action a", _action())])
    svc = Recorder(script=[("INSERT INTO auth_portal.auth_users", SimpleNamespace(id=NEW_UID))])
    r = _provision(tenant, svc, claim=False)
    assert r.status_code == 409
    assert not any("INSERT INTO auth_portal.auth_users" in q for q in svc.sqls())


# ── password reset: four-eyes, released once to the checker (db/043) ─────────
def test_reset_password_only_queues_four_eyes() -> None:
    t = Recorder(script=[
        ("FROM institution.member WHERE id",
         SimpleNamespace(id=MEMBER, is_primary_admin=False, auth_user_id=AUTH_UID)),
        ("FROM governance.action",
         SimpleNamespace(id=ACTION, status="pending", control="four_eyes")),
    ])
    r = _install(t).post(f"/members/{MEMBER}/reset-password")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval" and "temp_password" not in r.json()
    assert not any("auth_portal" in q for q in t.sqls())
    assert any(c[1].get("cat") == "user.reset_password" for c in t.calls)


def _release(tenant: Recorder, svc: Recorder) -> Any:
    tenant._script.append(("current_member_ctx", SimpleNamespace(id=CHECKER)))

    @contextmanager
    def _svc():  # noqa: ANN202
        yield svc

    client = _install(tenant)
    with patch("app.api.approvals.service_session", _svc):
        return client.post(f"/approvals/{ACTION}/release-password")


def test_checker_releases_reset_password_once() -> None:
    svc = Recorder(script=[
        ("credential_released_at = now()",
         SimpleNamespace(payload={"member_id": MEMBER}, institution_id=INST)),
        ("FROM institution.member m",
         SimpleNamespace(auth_user_id=AUTH_UID, email="a@b.mu", full_name="A B")),
        ("UPDATE auth_portal.auth_users", SimpleNamespace(id=AUTH_UID)),
    ])
    r = _release(Recorder(), svc)
    assert r.status_code == 200, r.text
    assert len(r.json()["temp_password"]) == 16
    claim = next(c for c in svc.calls if "credential_released_at = now()" in c[0])
    assert claim[1] == {"aid": ACTION, "cat": "user.reset_password", "me": CHECKER}
    assert any("INSERT INTO audit.event" in q for q in svc.sqls())


def test_anyone_else_or_second_release_gets_nothing() -> None:
    svc = Recorder()  # claim matches no row: not the checker, or already released
    r = _release(Recorder(), svc)
    assert r.status_code == 409
    assert not any("auth_portal" in q for q in svc.sqls())
