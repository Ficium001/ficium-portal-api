"""
Guards for the admin/identity schema consolidation (migrations 024+).

The old `admin` and `identity` schemas are being retired; platform staff and their groups now live
in `portal_admin`. These tests pin that contract so nothing quietly reads the old tables again.
No database required.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Objects that used to live in the retired schemas. `portal_admin.x` and `auth_portal.x` never match
# because the character before the schema name may not be a letter or underscore.
_OLD = re.compile(
    r"(?<![A-Za-z_])(?:admin|identity)\."
    r"(?:user|\"user\"|role|system_group|session|commission_event|notification_log|profile|"
    r"login_event|is_admin|has_permission|current_user_id|get_user_display_name)\b"
)


def test_no_code_reads_the_retired_admin_or_identity_schemas():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        for no, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]  # comments may still name the old tables
            if _OLD.search(code):
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{no}: {line.strip()}")
    assert not offenders, "Retired schemas referenced:\n" + "\n".join(offenders)


class _Result:
    def __init__(self, row=None, rows=None):
        self._row, self._rows = row, rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class RecordingSession:
    """Records the SQL it is asked to run and answers from a queue of canned results."""

    def __init__(self, *results: _Result):
        self.sql: list[str] = []
        self._results = list(results)

    def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        return self._results.pop(0) if self._results else _Result()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _claims(**kw):
    base = {
        "sub": "4224540d-d59c-4584-8271-cb6ef24c472d",
        "role": "authenticated",
        "user_role": "super_admin",
    }
    base.update(kw)

    async def _dep():
        return base

    return _dep


def _conn(session):
    def _dep(claims=None):
        yield session

    return _dep


def test_platform_admin_group_comes_from_portal_admin():
    grp = {"slug": "super_admin", "module_permissions": ["*"], "user_type": "admin"}
    session = RecordingSession(_Result(row=SimpleNamespace(grp=grp)))
    app.dependency_overrides[current_claims] = _claims()
    app.dependency_overrides[tenant_conn] = _conn(session)
    try:
        r = TestClient(app, raise_server_exceptions=False).get("/members/my-group")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json()["module_permissions"] == ["*"]
    sql = " ".join(session.sql)
    assert "portal_admin.admin_users" in sql and "portal_admin.user_groups" in sql
    assert "u.group_id" in sql  # the new link; the old one was system_group_id


def test_member_login_history_reads_auth_portal_and_keeps_its_shape():
    member = SimpleNamespace(
        auth_user_id="aaaaaaaa-0000-0000-0000-000000000001",
        email="m@example.invalid",
        full_name="M",
        active=True,
    )
    login = SimpleNamespace(
        _mapping={
            "id": "e1",
            "email": "m@example.invalid",
            "ip": "10.0.0.1",
            "user_agent": "UA",
            "country": None,
            "city": None,
            "outcome": "success",
            "failure_reason": None,
            "occurred_at": datetime(2026, 10, 5, 14, 33, tzinfo=UTC),
        }
    )
    tenant = RecordingSession(_Result(row=member), _Result(rows=[]), _Result(rows=[]))
    service = RecordingSession(_Result(rows=[login]))

    @contextmanager
    def _svc():
        yield service

    app.dependency_overrides[current_claims] = _claims(user_role="institution_admin")
    app.dependency_overrides[tenant_conn] = _conn(tenant)
    try:
        with patch("app.api.members.service_session", side_effect=_svc):
            url = "/members/11111111-1111-1111-1111-111111111111/audit"
            r = TestClient(app, raise_server_exceptions=False).get(url)
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["logins"]) == 1
    entry = body["logins"][0]
    assert set(entry) == {
        "id", "email", "ip", "user_agent", "country", "city",
        "outcome", "failure_reason", "occurred_at",
    }
    assert entry["outcome"] == "success" and entry["occurred_at"].startswith("2026-10-05")
    sql = " ".join(service.sql)
    assert "auth_portal.auth_audit_events" in sql and "'login.success', 'login.failed'" in sql
    assert not any("auth_portal" in q for q in tenant.sql), "tenant must not touch auth_portal"
