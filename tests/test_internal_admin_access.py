"""Admin channel: institution applications and four-eyes provisioning (db/060).

Review is one person (the review is the check). Provisioning is four-eyes and is
the only thing that creates an institution and its first admin login, and that
login cannot sign in until the invitee sets a password from ficium-auth's link.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import internal_admin
from app.core.config import settings
from app.main import app
from tests.test_internal_admin import CHECKER, KEY, MAKER, FakeConn, _Res, call, cmd, staff

REQ = "55555555-5555-5555-5555-555555555555"
NEW_INST = "66666666-6666-6666-6666-666666666666"


class AccessConn(FakeConn):
    def __init__(
        self, status: str = "submitted", email_taken: bool = False, dup: bool = False,
    ) -> None:
        super().__init__()
        self.email_taken, self.dup = email_taken, dup
        self.req = {
            "id": REQ, "reference": "AB12CD34EF", "institution_name": "ZZ Bank",
            "institution_type": "commercial_bank", "country": "MU", "reg_number": "C1234",
            "regulator": "Bank of Mauritius", "website": "https://zzbank.mu",
            "contact_name": "Ada Lovelace", "contact_email": "Ada@ZZBank.mu", "contact_phone": None,
            "status": status, "verified_at": datetime.now(UTC),
        }

    def execute(self, stmt: Any, params: dict | None = None) -> _Res:
        q, p = " ".join(str(stmt).split()), params or {}
        if "FROM institution.access_request WHERE id" in q:
            self.sql.append((q, p))
            return _Res([SimpleNamespace(_mapping=dict(self.req))])
        if "FROM institution.access_request WHERE status = ANY" in q:
            self.sql.append((q, p))
            row = {"request_id": REQ, "status": "submitted"}
            return _Res([SimpleNamespace(_mapping=row)])
        if "FROM auth_portal.auth_users WHERE lower(email)" in q:
            self.sql.append((q, p))
            return _Res([(1,)] if self.email_taken else [])
        if "FROM institution.institution WHERE lower(name)" in q:
            self.sql.append((q, p))
            return _Res([(1,)] if self.dup else [])
        if q.startswith("INSERT INTO institution.institution"):
            self.sql.append((q, p))
            return _Res([SimpleNamespace(id=uuid.UUID(NEW_INST))])
        if q.startswith("UPDATE institution.access_request"):
            self.sql.append((q, p))
            self.req["status"] = p.get("to", "provisioned")
            return _Res([])
        return super().execute(stmt, params)

    def nsql(self) -> list[tuple[str, dict]]:
        return [(" ".join(q.split()), p) for q, p in self.sql]

    def created(self) -> list[str]:
        skip = ("audit.event", "admin_command_log")
        return [q.split(" (")[0] for q, _ in self.nsql()
                if q.startswith("INSERT INTO") and not any(s in q for s in skip)]


@contextmanager
def chan(conn: AccessConn):
    @contextmanager
    def svc():
        yield conn

    with (
        patch.object(settings, "admin_command_verify_keys", KEY.decode()),
        patch.object(internal_admin, "service_session", svc),
        patch.object(internal_admin, "verify_token", AsyncMock(return_value=staff())),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def review(status: str, note: str | None = None, maker: str = CHECKER) -> dict:
    return cmd("access_request.review", {"request_id": REQ, "status": status,
                                          **({"note": note} if note else {})}, maker=maker)


def provision(maker: str = MAKER, **over: Any) -> dict:
    return cmd("institution.provision", {"request_id": REQ, "contract_reference": "MSA-2026-014",
                                         "deployment_model": "saas", **over}, maker=maker)


# ── review: one person ──────────────────────────────────────────────────────────

def test_review_is_single_control_and_audited() -> None:
    conn = AccessConn()
    with chan(conn) as c:
        r = call(c, "commands", review("in_review"))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "in_review" and conn.req["status"] == "in_review"
    upd = next(p for q, p in conn.nsql() if q.startswith("UPDATE institution.access_request"))
    assert upd["by"] == CHECKER
    assert conn.actions() == ["access_request.in_review"]


def test_review_maker_must_be_the_actor() -> None:
    conn = AccessConn()
    with chan(conn) as c:
        r = call(c, "commands", review("in_review", maker=MAKER))
    assert r.status_code == 422 and conn.req["status"] == "submitted"


@pytest.mark.parametrize("start,to", [("rejected", "approved"), ("provisioned", "rejected"),
                                       ("submitted", "provisioned"), ("approved", "approved")])
def test_review_refuses_impossible_moves(start: str, to: str) -> None:
    conn = AccessConn(status=start)
    with chan(conn) as c:
        r = call(c, "commands", review(to, note="A long enough note"))
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition"
    assert conn.req["status"] == start


@pytest.mark.parametrize("to", ["rejected", "info_requested"])
def test_reject_and_ask_for_info_need_a_note(to: str) -> None:
    conn = AccessConn()
    with chan(conn) as c:
        r = call(c, "commands", review(to, note="short"))
    assert r.status_code == 422 and conn.req["status"] == "submitted"


def test_unconfirmed_applications_do_not_exist_for_staff() -> None:
    conn = AccessConn(status="unverified")
    with chan(conn) as c:
        r = call(c, "commands", review("in_review"))
    assert r.status_code == 404


def test_list_never_asks_for_unconfirmed_ones_and_validates_the_filter() -> None:
    conn = AccessConn()
    with chan(conn) as c:
        ok = call(c, "query", {"op": "access_requests.list", "args": {}})
        bad = call(c, "query", {"op": "access_requests.list", "args": {"status": "unverified"}})
    assert ok.status_code == 200 and bad.status_code == 422
    asked = next(p for q, p in conn.nsql() if "WHERE status = ANY" in q)["st"]
    assert "unverified" not in asked and "expired" not in asked


# ── provision: four-eyes, the only path that creates anything ─────────────────────

def test_provision_is_four_eyes() -> None:
    conn = AccessConn(status="approved")
    with chan(conn) as c:
        r = call(c, "commands", provision(maker=CHECKER))
    assert r.status_code == 403 and r.json()["error"] == "four_eyes" and conn.created() == []


def test_provision_needs_an_approved_application() -> None:
    conn = AccessConn(status="in_review")
    with chan(conn) as c:
        r = call(c, "commands", provision())
    assert r.status_code == 409 and r.json()["error"] == "not_approved" and conn.created() == []


@pytest.mark.parametrize("over", [{"contract_reference": ""}, {"contract_reference": "x" * 101},
                                  {"deployment_model": "cloud"}])
def test_provision_validates_contract_and_model(over: dict) -> None:
    conn = AccessConn(status="approved")
    with chan(conn) as c:
        r = call(c, "commands", provision(**over))
    assert r.status_code == 422 and conn.created() == []


def test_provision_creates_unapproved_bank_unusable_login_and_primary_admin() -> None:
    conn = AccessConn(status="approved")
    with chan(conn) as c:
        r = call(c, "commands", provision())
    assert r.status_code == 200, r.text
    assert conn.created() == [
        "INSERT INTO institution.institution",
        "INSERT INTO auth_portal.auth_users",
        "INSERT INTO institution.member",
    ]
    q_inst = next(q for q, _ in conn.nsql() if q.startswith("INSERT INTO institution.institution"))
    assert "'registered', 'not_submitted', false" in q_inst          # unapproved, onboarding starts
    login = next(p for q, p in conn.nsql() if q.startswith("INSERT INTO auth_portal.auth_users"))
    assert login["pw"] == "!invited-not-activated" and login["email"] == "ada@zzbank.mu"
    assert login["iid"] == NEW_INST
    q_login = next(q for q, _ in conn.nsql() if q.startswith("INSERT INTO auth_portal.auth_users"))
    assert "'institution_admin'" in q_login
    member = next(q for q, _ in conn.nsql() if q.startswith("INSERT INTO institution.member"))
    assert "'admin', true, true" in member                              # primary admin, active
    upd = next(p for q, p in conn.nsql() if q.startswith("UPDATE institution.access_request"))
    assert upd["by"] == CHECKER and upd["iid"] == NEW_INST and upd["contract"] == "MSA-2026-014"
    assert conn.actions() == ["institution.provisioned", "access_request.provisioned"]
    data = r.json()["data"]
    assert data["institution_id"] == NEW_INST and data["admin_email"] == "ada@zzbank.mu"
    assert "!invited" not in r.text  # the marker never leaves the API


@pytest.mark.parametrize("kw,code", [
    ({"email_taken": True}, "email_in_use"),
    ({"dup": True}, "institution_exists"),
])
def test_provision_never_reuses_an_email_or_duplicates_a_bank(kw: dict, code: str) -> None:
    conn = AccessConn(status="approved", **kw)
    with chan(conn) as c:
        r = call(c, "commands", provision())
    assert r.status_code == 409 and r.json()["error"] == code and conn.created() == []


def test_provision_replay_returns_the_same_result_and_creates_nothing_more() -> None:
    conn = AccessConn(status="approved")
    body = provision()
    with chan(conn) as c:
        first = call(c, "commands", body)
        again = call(c, "commands", body)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert conn.created().count("INSERT INTO institution.institution") == 1
