"""Admin command channel: signature AND staff MFA token, four-eyes, idempotency, audit."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

import ficium_contract as fc  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import internal_admin  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.security import TokenError  # noqa: E402
from app.main import app  # noqa: E402

KEY = b"test-admin-signing-key"
PLATFORM = "00000000-0000-0000-0000-000000000001"
CHECKER = "11111111-1111-1111-1111-111111111111"
MAKER = "22222222-2222-2222-2222-222222222222"
INST = "33333333-3333-3333-3333-333333333333"
DOC = "44444444-4444-4444-4444-444444444444"


def staff(**over: Any) -> dict:
    c = {
        "sub": CHECKER,
        "institution_id": PLATFORM,
        "user_role": "super_admin",
        "amr": ["pwd", "mfa"],
    }
    c.update(over)
    return c


class _Res:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return self.rows


class _Nested:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        self.conn.rolled_back += 1


class FakeConn:
    """Answers by keyword; remembers the command log so replays can be tested."""

    def __init__(self) -> None:
        self.sql: list[tuple[str, dict]] = []
        self.log: dict[str, SimpleNamespace] = {}
        self.rolled_back = 0
        self.doc_exists = True

    def begin_nested(self) -> _Nested:
        return _Nested(self)

    def execute(self, stmt: Any, params: dict | None = None) -> _Res:
        q, p = str(stmt), params or {}
        self.sql.append((q, p))
        if "FROM integration.admin_command_log" in q:
            hit = self.log.get(p["k"])
            return _Res([hit] if hit else [])
        if "INSERT INTO integration.admin_command_log" in q:
            self.log[p["k"]] = SimpleNamespace(
                body_sha256=p["h"], status_code=p["s"], response=json.loads(p["r"])
            )
            return _Res([])
        if "FROM institution.compliance c WHERE" in q:
            return _Res(
                [
                    SimpleNamespace(
                        _mapping={
                            "institution_id": p["iid"],
                            "can_bid": True,
                            "missing_docs": [],
                            "waived_until": None,
                        }
                    )
                ]
            )
        if "UPDATE institution.doc" in q:
            if not self.doc_exists:
                return _Res([])
            return _Res(
                [
                    SimpleNamespace(
                        _mapping={
                            "id": p["id"],
                            "institution_id": INST,
                            "status": p["status"],
                            "rejection_reason": p["reason"],
                            "expiry_date": None,
                            "reviewed_at": datetime.now(UTC),
                        }
                    )
                ]
            )
        if "UPDATE institution.compliance_waiver" in q:
            return _Res([SimpleNamespace(institution_id=p["iid"])])
        return _Res([])

    def actions(self) -> list[str]:
        return [p.get("action") for q, p in self.sql if "INSERT INTO audit.event" in q]


@contextmanager
def channel(claims: dict | Exception | None = None, keys: str = KEY.decode()):
    conn = FakeConn()

    @contextmanager
    def svc():
        yield conn

    verify = (
        AsyncMock(side_effect=claims)
        if isinstance(claims, Exception)
        else AsyncMock(return_value=claims or staff())
    )
    with (
        patch.object(settings, "admin_command_verify_keys", keys),
        patch.object(internal_admin, "service_session", svc),
        patch.object(internal_admin, "verify_token", verify),
    ):
        yield TestClient(app, raise_server_exceptions=False), conn


def call(
    c: TestClient,
    path: str,
    body: dict,
    *,
    key: bytes = KEY,
    actor: str | None = "tok",
    raw: bytes | None = None,
):
    data = raw if raw is not None else json.dumps(body).encode()
    headers = {fc.SIGNATURE_HEADER: fc.sign(data, key), "Content-Type": "application/json"}
    if actor is not None:
        headers[internal_admin.ACTOR_HEADER] = actor
    return c.post(f"/internal/admin/v1/{path}", content=data, headers=headers)


def grant(command_id: str | None = None, maker: str = MAKER, days: int = 30) -> dict:
    until = (datetime.now(UTC) + timedelta(days=days)).isoformat()
    return {
        "op": "waiver.grant",
        "command_id": command_id or str(uuid.uuid4()),
        "maker_sub": maker,
        "args": {
            "institution_id": INST,
            "waived_until": until,
            "reason": "MCB documents in legal review",
        },
    }


# ── authentication ────────────────────────────────────────────────────────────


def test_channel_off_without_keys() -> None:
    with channel(keys="") as (c, _):
        assert call(c, "query", {"op": "compliance.overview"}).status_code == 503


def test_wrong_key_is_refused() -> None:
    with channel() as (c, conn):
        r = call(c, "commands", grant(), key=b"someone-else")
    assert r.status_code == 401 and r.json()["error"] == "bad_signature"
    assert conn.sql == []


def test_signature_alone_is_not_enough() -> None:
    with channel() as (c, _):
        assert call(c, "commands", grant(), actor=None).json()["error"] == "actor_required"
    with channel(TokenError("expired")) as (c, _):
        assert call(c, "commands", grant()).json()["error"] == "bad_actor"


@pytest.mark.parametrize(
    "claims,code",
    [
        (staff(institution_id=INST), "not_staff"),  # a bank user's token
        (staff(user_role="admin"), "not_staff"),
        (staff(amr=["pwd"]), "mfa_required"),
    ],
)
def test_only_platform_staff_with_mfa(claims: dict, code: str) -> None:
    with channel(claims) as (c, conn):
        r = call(c, "commands", grant())
    assert r.status_code == 403 and r.json()["error"] == code
    assert conn.sql == []


def test_tampered_body_fails_signature() -> None:
    body = json.dumps(grant()).encode()
    with channel() as (c, _):
        headers = {fc.SIGNATURE_HEADER: fc.sign(body, KEY), internal_admin.ACTOR_HEADER: "tok"}
        tampered = body.replace(b"waiver.grant", b"waiver.revoke")
        r = c.post("/internal/admin/v1/commands", content=tampered, headers=headers)
    assert r.status_code == 401


# ── commands ──────────────────────────────────────────────────────────────────


def test_four_eyes_approver_cannot_be_maker() -> None:
    with channel() as (c, conn):
        r = call(c, "commands", grant(maker=CHECKER))
    assert r.status_code == 403 and r.json()["error"] == "four_eyes"
    assert not any("compliance_waiver" in q for q, _ in conn.sql)


def test_waiver_grant_is_audited_and_logged() -> None:
    body = grant()
    with channel() as (c, conn):
        r = call(c, "commands", body)
    assert r.status_code == 200, r.text
    assert conn.actions() == ["compliance.waiver_granted"]
    assert conn.log[body["command_id"]].status_code == 200


def test_replay_returns_stored_result_and_changes_nothing() -> None:
    body = grant()
    with channel() as (c, conn):
        first = call(c, "commands", body)
        writes_after_first = sum("compliance_waiver" in q for q, _ in conn.sql)
        second = call(c, "commands", body)
    assert first.json() == second.json()
    assert sum("compliance_waiver" in q for q, _ in conn.sql) == writes_after_first


def test_reused_command_id_with_other_body_is_refused() -> None:
    cid = str(uuid.uuid4())
    with channel() as (c, _):
        call(c, "commands", grant(cid))
        r = call(c, "commands", grant(cid, days=60))
    assert r.status_code == 409


def test_waiver_over_180_days_is_refused_and_recorded() -> None:
    body = grant(days=200)
    with channel() as (c, conn):
        r = call(c, "commands", body)
    assert r.status_code == 422
    assert conn.rolled_back == 1 and conn.log[body["command_id"]].status_code == 422
    assert conn.actions() == []


def test_document_review_is_single_control_and_records_reviewer() -> None:
    body = {
        "op": "document.review",
        "command_id": str(uuid.uuid4()),
        "maker_sub": CHECKER,
        "args": {"doc_id": DOC, "decision": "approve"},
    }
    with channel() as (c, conn):
        r = call(c, "commands", body)
    assert r.status_code == 200, r.text
    upd = next(p for q, p in conn.sql if "UPDATE institution.doc" in q)
    assert upd["by"] == CHECKER and upd["status"] == "approved"
    assert conn.actions() == ["compliance.document_approved"]


def test_reject_needs_a_reason() -> None:
    body = {
        "op": "document.review",
        "command_id": str(uuid.uuid4()),
        "maker_sub": CHECKER,
        "args": {"doc_id": DOC, "decision": "reject"},
    }
    with channel() as (c, _):
        assert call(c, "commands", body).status_code == 422


def test_unknown_op() -> None:
    with channel() as (c, _):
        assert (
            call(
                c,
                "commands",
                {"op": "institution.delete", "command_id": str(uuid.uuid4()), "maker_sub": CHECKER},
            ).status_code
            == 404
        )
        assert call(c, "query", {"op": "members.export"}).status_code == 404


# ── queries ───────────────────────────────────────────────────────────────────


def test_document_download_is_audited() -> None:
    with channel() as (c, conn):
        orig = conn.execute

        def ex(stmt: Any, params: dict | None = None) -> _Res:
            if "SELECT institution_id, storage_path" in str(stmt):
                conn.sql.append((str(stmt), params or {}))
                return _Res(
                    [
                        SimpleNamespace(
                            institution_id=INST, storage_path="docs/x.pdf", file_name="x.pdf"
                        )
                    ]
                )
            return orig(stmt, params)

        conn.execute = ex  # type: ignore[method-assign]
        with patch("app.core.storage.storage_signed_url", AsyncMock(return_value="https://signed")):
            r = call(c, "query", {"op": "document.download", "args": {"doc_id": DOC}})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["url"] == "https://signed"
    assert conn.actions() == ["compliance.document_viewed"]


# ── institutions & entitlements ───────────────────────────────────────────────


class InstConn(FakeConn):
    """FakeConn plus an institution row and the module catalogue."""

    def __init__(self, approved: bool = False, stage: str = "pending") -> None:
        super().__init__()
        self.inst = {
            "institution_id": INST,
            "name": "MCB",
            "approved": approved,
            "onboarding_stage": stage,
            "approved_at": None,
            "suspended_at": None,
            "suspension_reason": None,
        }

    def execute(self, stmt: Any, params: dict | None = None) -> _Res:
        q, p = str(stmt), params or {}
        if "FROM institution.institution WHERE id" in q:
            self.sql.append((q, p))
            return _Res([SimpleNamespace(_mapping=dict(self.inst))])
        if "UPDATE institution.institution" in q:
            self.sql.append((q, p))
            self.inst["approved"] = "approved = true" in q
            self.inst["onboarding_stage"] = "approved" if "approved = true" in q else "suspended"
            return _Res([])
        if "FROM entitlements.module WHERE module_code" in q:
            self.sql.append((q, p))
            return _Res([SimpleNamespace()] if p["c"] == "AUTOBID" else [])
        if "INSERT INTO entitlements.institution_entitlement" in q:
            self.sql.append((q, p))
            row = {
                "institution_id": INST,
                "module_code": p["c"],
                "plan_tier": p["tier"],
                "status": p["status"],
                "valid_from": datetime.now(UTC),
                "valid_to": p["valid_to"],
            }
            return _Res([SimpleNamespace(_mapping=row)])
        return super().execute(stmt, params)


@contextmanager
def inst_channel(conn: InstConn):
    @contextmanager
    def svc():
        yield conn

    with (
        patch.object(settings, "admin_command_verify_keys", KEY.decode()),
        patch.object(internal_admin, "service_session", svc),
        patch.object(internal_admin, "verify_token", AsyncMock(return_value=staff())),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def cmd(op: str, args: dict, maker: str = MAKER) -> dict:
    return {"op": op, "command_id": str(uuid.uuid4()), "maker_sub": maker, "args": args}


@pytest.mark.parametrize(
    "op,args",
    [
        ("institution.approve", {"institution_id": INST}),
        ("institution.suspend", {"institution_id": INST, "reason": "Licence under review"}),
        ("entitlement.set", {"institution_id": INST, "module_code": "AUTOBID", "status": "active"}),
    ],
)
def test_institution_ops_are_four_eyes(op: str, args: dict) -> None:
    conn = InstConn()
    with inst_channel(conn) as c:
        r = call(c, "commands", cmd(op, args, maker=CHECKER))
    assert r.status_code == 403 and r.json()["error"] == "four_eyes"
    assert not any(
        "UPDATE institution.institution" in q or "INSERT INTO entitlements" in q
        for q, _ in conn.sql
    )


def test_approve_sets_approver_and_audits() -> None:
    conn = InstConn()
    with inst_channel(conn) as c:
        r = call(c, "commands", cmd("institution.approve", {"institution_id": INST}))
    assert r.status_code == 200, r.text
    upd = next(p for q, p in conn.sql if "UPDATE institution.institution" in q)
    assert upd["by"] == CHECKER
    assert conn.actions() == ["institution.approved"]


def test_approving_an_approved_bank_is_refused() -> None:
    conn = InstConn(approved=True, stage="approved")
    with inst_channel(conn) as c:
        r = call(c, "commands", cmd("institution.approve", {"institution_id": INST}))
    assert r.status_code == 409 and conn.actions() == []


def test_suspend_needs_reason_and_records_it() -> None:
    conn = InstConn(approved=True, stage="approved")
    with inst_channel(conn) as c:
        assert (
            call(
                c, "commands", cmd("institution.suspend", {"institution_id": INST, "reason": "no"})
            ).status_code
            == 422
        )
        r = call(
            c,
            "commands",
            cmd(
                "institution.suspend",
                {"institution_id": INST, "reason": "Licence under review by regulator"},
            ),
        )
    assert r.status_code == 200, r.text
    assert conn.actions() == ["institution.suspended"]


@pytest.mark.parametrize(
    "args,code",
    [
        ({"institution_id": INST, "module_code": "NOPE", "status": "active"}, 422),
        ({"institution_id": INST, "module_code": "AUTOBID", "status": "forever"}, 422),
        (
            {
                "institution_id": INST,
                "module_code": "AUTOBID",
                "status": "active",
                "plan_tier": "gold",
            },
            422,
        ),
        (
            {
                "institution_id": INST,
                "module_code": "AUTOBID",
                "status": "active",
                "valid_to": "2020-01-01T00:00:00Z",
            },
            422,
        ),
    ],
)
def test_entitlement_validation(args: dict, code: int) -> None:
    conn = InstConn()
    with inst_channel(conn) as c:
        assert call(c, "commands", cmd("entitlement.set", args)).status_code == code
    assert not any("INSERT INTO entitlements" in q for q, _ in conn.sql)


def test_entitlement_set_upserts_and_audits_before_after() -> None:
    conn = InstConn()
    with inst_channel(conn) as c:
        r = call(
            c,
            "commands",
            cmd(
                "entitlement.set",
                {
                    "institution_id": INST,
                    "module_code": "AUTOBID",
                    "status": "trial",
                    "plan_tier": "trial",
                },
            ),
        )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "trial"
    meta = json.loads(next(p for q, p in conn.sql if "INSERT INTO audit.event" in q)["meta"])
    assert meta["module_code"] == "AUTOBID" and "after" in meta and meta["before"] is None
