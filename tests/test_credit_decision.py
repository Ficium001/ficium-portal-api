"""
Credit decision irrespective of system (db/029).

The rules (outcome/conditions/reason/reference validation, stage must be the
active credit stage, external-only, tenant isolation, approve -> next stage,
refer back -> stage stays open, decline -> deal declined) live in
marketplace.record_credit_decision() and were verified against the live Portal
DB on a real deal in an aborted transaction before db/029 was applied. These
tests cover what Python owns: permissions, scope checks, validation, error
mapping, side effects, and the two bypasses closed in the same change.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.core import credit_decision as svc  # noqa: E402
from app.deps import api_or_jwt_claims, current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

INST = "31b3ee32-2864-4875-8920-cc5f27240971"
USER = "4224540d-d59c-4584-8271-cb6ef24c472d"
DEAL = "5a0c7c1e-1111-4222-8333-944455556666"
STAGE = "6b1d8d2f-2222-4333-8444-a55566667777"

APPROVE = {"outcome": "approved", "external_reference": "CR-2026-0042"}


def _result(outcome: str = "approved", deal_status: str = "active", **over: Any) -> dict:
    return {"id": "d-1", "pipeline_id": DEAL, "stage_id": STAGE, "outcome": outcome,
            "conditions": over.get("conditions", []), "reason": over.get("reason"),
            "external_reference": "CR-2026-0042", "decided_at": "2026-10-17T09:00:00Z",
            "source": over.get("source", "portal"), "deal_status": deal_status,
            "next_stage_id": None}


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    @property
    def _mapping(self) -> dict:
        return self.__dict__


class _Result:
    def __init__(self, scalar: Any = None, first: Any = None, rows: list | None = None) -> None:
        self._scalar, self._first, self._rows = scalar, first, rows or []

    def scalar_one(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._first

    def fetchone(self) -> Any:
        return self._first

    def fetchall(self) -> list:
        return self._rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list:
        return self._rows


class FakeSession:
    def __init__(self, fn_result: Any = None, fn_error: Exception | None = None,
                 modules: list[str] | None = None, stage: _Row | None = None) -> None:
        self.fn_result, self.fn_error = fn_result, fn_error
        self.modules, self.stage = modules, stage
        self.sql: list[str] = []
        self.committed = self.rolled_back = False

    def execute(self, stmt: Any, params: Any = None) -> _Result:
        q = str(stmt)
        self.sql.append(q)
        if "record_credit_decision" in q:
            if self.fn_error:
                raise self.fn_error
            return _Result(scalar=self.fn_result)
        if "FROM institution.institution" in q:
            return _Result(first=_Row(modules=["pipeline"]))
        if "FROM marketplace.loan_pipeline" in q:
            return _Result(first=_Row(id=DEAL, status="active", current_stage_id=STAGE))
        if "FROM marketplace.pipeline_stage_instance psi" in q and self.stage is not None:
            return _Result(first=self.stage)
        if "modules" in q:
            return _Result(first=_Row(modules=self.modules or []))
        return _Result()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _db_error(code: str) -> DBAPIError:
    return DBAPIError("SELECT ...", {}, Exception(f"ERROR: {code}\nCONTEXT: PL/pgSQL"))


# ── service ──────────────────────────────────────────────────────────────────
def test_every_db_code_maps() -> None:
    for code, (status, _) in svc._ERRORS.items():
        mapped = svc._credit_error(_db_error(code))
        assert mapped is not None and mapped.status_code == status, code


def test_internal_stage_is_a_conflict() -> None:
    mapped = svc._credit_error(_db_error("CREDIT_STAGE_INTERNAL"))
    assert mapped is not None and mapped.status_code == 409


def test_unknown_db_error_is_not_swallowed() -> None:
    assert svc._credit_error(_db_error("deadlock detected")) is None


@pytest.mark.parametrize(("modules", "role", "ok"), [
    (["inst:approvals"], "checker", True),
    (["*"], "maker", True),
    (["inst:pipeline", "inst:booking"], "maker", False),
    ([], "institution_super_admin", True),
    (None, "maker", False),
])
def test_permission(modules: list[str] | None, role: str, ok: bool) -> None:
    assert svc.has_decision_permission(modules, role) is ok


def test_conditions_are_trimmed_and_blanks_dropped() -> None:
    body = svc.CreditDecisionInput(outcome="approved_with_conditions", external_reference="X",
                                   conditions=["  Life cover  ", "", "   "])
    assert body.conditions == ["Life cover"]


def _record(session: FakeSession, body: dict | None = None) -> tuple[dict, BackgroundTasks]:
    bg = BackgroundTasks()
    out = svc.record_decision(
        session,  # type: ignore[arg-type]
        institution_id=INST, pipeline_id=DEAL,
        body=svc.CreditDecisionInput(**(body or APPROVE)), source="portal",
        actor_id=USER, actor_email="a@b.mu", api_key_id=None, background=bg,
    )
    return out, bg


def _events(bg: BackgroundTasks) -> list[str]:
    return [t.args[1] for t in bg.tasks if len(t.args) > 1]


def test_approval_audits_commits_and_fires_the_webhook() -> None:
    s = FakeSession(fn_result=_result())
    _, bg = _record(s)
    assert s.committed and any("audit.event" in q for q in s.sql)
    assert _events(bg) == ["deal.credit_decided"]
    # The borrower is told by deal.progress_updated (db/033), not a direct call.
    assert len(bg.tasks) == 1


def test_decline_also_fires_deal_closed_and_no_borrower_advance() -> None:
    s = FakeSession(fn_result=_result("declined", "declined", reason="Affordability"))
    _, bg = _record(s, {"outcome": "declined", "external_reference": "CR-1",
                        "reason": "Affordability"})
    assert _events(bg) == ["deal.credit_decided", "deal.closed"]
    assert len(bg.tasks) == 2


def test_refer_back_only_fires_decided() -> None:
    s = FakeSession(fn_result=_result("referred_back", reason="Need payslip"))
    _, bg = _record(s, {"outcome": "referred_back", "external_reference": "CR-1",
                        "reason": "Need payslip"})
    assert _events(bg) == ["deal.credit_decided"] and len(bg.tasks) == 1


def test_rule_violation_rolls_back_and_maps() -> None:
    s = FakeSession(fn_error=_db_error("CREDIT_STAGE_NOT_ACTIVE"))
    with pytest.raises(HTTPException) as e:
        _record(s)
    assert e.value.status_code == 409
    assert s.rolled_back and not s.committed


# ── portal ───────────────────────────────────────────────────────────────────
@contextmanager
def _portal(modules: list[str], fn_result: Any = None, role: str = "checker",
            stage: _Row | None = None):
    session = FakeSession(fn_result=fn_result, modules=modules, stage=stage)

    async def claims() -> dict[str, Any]:
        return {"sub": USER, "institution_id": INST, "user_role": role, "email": "a@b.mu"}

    def conn(claims: Any = None):
        yield session

    @contextmanager
    def service():
        yield session

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = conn
    try:
        with patch("app.deps.service_session", service), \
             patch("app.api.pipeline.service_session", service), \
             patch("app.api.pipeline_templates.service_session", service), \
             patch("app.core.credit_decision.dispatch_event"):
            yield TestClient(app, raise_server_exceptions=False), session
    finally:
        app.dependency_overrides.clear()


def test_portal_requires_approvals_permission() -> None:
    with _portal(["inst:pipeline", "inst:booking"]) as (client, _):
        r = client.post(f"/pipelines/{DEAL}/credit-decision", json=APPROVE)
    assert r.status_code == 403


def test_portal_records_decision() -> None:
    with _portal(["inst:approvals"], fn_result=_result()) as (client, s):
        r = client.post(f"/pipelines/{DEAL}/credit-decision", json=APPROVE)
    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "approved"
    assert any("record_credit_decision" in q for q in s.sql)


@pytest.mark.parametrize("bad", [
    {"outcome": "approved"},                                   # no reference
    {**APPROVE, "external_reference": "x" * 65},
    {**APPROVE, "outcome": "maybe"},
    {**APPROVE, "conditions": ["x" * 501]},
])
def test_portal_validates_body(bad: dict[str, Any]) -> None:
    with _portal(["inst:approvals"]) as (client, _):
        r = client.post(f"/pipelines/{DEAL}/credit-decision", json=bad)
    assert r.status_code == 422


def test_portal_advance_refuses_external_credit_stage() -> None:
    stage = _Row(id=STAGE, status="active", requires_maker_checker=False, approval_route=True,
                 decision_source="external", label="Credit", position=4)
    with _portal(["inst:pipeline"], stage=stage) as (client, s):
        r = client.post(f"/pipelines/{DEAL}/stages/{STAGE}/advance", json={})
    assert r.status_code == 409
    assert not any("approvals_route" in q for q in s.sql)


@pytest.mark.parametrize("method,path", [
    ("post", "/pipelines/templates"),
    ("put", "/pipelines/templates/t-1"),
    ("post", "/pipelines/templates/t-1/stages"),
    ("put", "/pipelines/templates/t-1/stages/s-1"),
    ("delete", "/pipelines/templates/t-1/stages/s-1"),
])
def test_workflow_edits_need_admin(method: str, path: str) -> None:
    with _portal(["inst:pipeline"], role="maker") as (client, _):
        r = client.request(method.upper(), path, json={"decision_source": "external"})
    assert r.status_code == 403


def test_decision_source_is_validated() -> None:
    with _portal(["inst:pipeline"], role="institution_admin") as (client, _):
        r = client.put("/pipelines/templates/t-1/stages/s-1", json={"decision_source": "bank"})
    # 404 (stage lookup) happens first in the fake; validate directly instead.
    from app.api.pipeline_templates import _decision_source
    with pytest.raises(HTTPException) as e:
        _decision_source("bank", None)
    assert e.value.status_code == 422
    assert _decision_source(None, None) is None
    assert _decision_source(None, "internal") == "internal"
    assert r.status_code in (404, 422)


# ── v1 ───────────────────────────────────────────────────────────────────────
@contextmanager
def _v1(claims: dict[str, Any], modules: list[str] | None = None, fn_result: Any = None,
        stage: _Row | None = None):
    session = FakeSession(fn_result=fn_result, modules=modules, stage=stage)

    async def dep() -> dict[str, Any]:
        return claims

    @contextmanager
    def service():
        yield session

    app.dependency_overrides[api_or_jwt_claims] = dep
    try:
        with patch("app.api.v1.marketplace._svc", service), \
             patch("app.core.credit_decision.dispatch_event"), \
             patch("app.api.v1.marketplace.dispatch_event"):
            yield TestClient(app, raise_server_exceptions=False), session
    finally:
        app.dependency_overrides.clear()


API_KEY = {"auth_method": "api_key", "institution_id": INST, "key_id": "k-1",
           "scopes": ["pipeline:write"]}


def test_v1_requires_pipeline_write_scope() -> None:
    with _v1({**API_KEY, "scopes": ["pipeline:read"]}) as (client, _):
        r = client.post(f"/v1/pipeline/{DEAL}/credit-decision", json=APPROVE)
    assert r.status_code == 403


def test_v1_api_key_records_as_api_source() -> None:
    with _v1(API_KEY, fn_result=_result(source="api")) as (client, _), \
         patch("app.api.v1.marketplace.record_decision", wraps=svc.record_decision) as spy:
        r = client.post(f"/v1/pipeline/{DEAL}/credit-decision", json=APPROVE)
    assert r.status_code == 201, r.text
    assert r.json()["loan_id"] == DEAL
    kw = spy.call_args.kwargs
    assert kw["source"] == "api" and kw["api_key_id"] == "k-1" and kw["actor_id"] is None


def test_v1_jwt_user_needs_approvals_permission() -> None:
    jwt = {"auth_method": "jwt", "institution_id": INST, "sub": USER, "user_role": "maker"}
    with _v1(jwt, modules=["inst:pipeline"]) as (client, _):
        r = client.post(f"/v1/pipeline/{DEAL}/credit-decision", json=APPROVE)
    assert r.status_code == 403


@pytest.mark.parametrize("source", ["internal", "external"])
def test_v1_advance_cannot_tick_through_credit_stage(source: str) -> None:
    """The bypass: PUT .../advance used to complete the credit stage directly."""
    stage = _Row(id=STAGE, status="active", position=4, stage_key="board_approval",
                 borrower_label=None, requires_maker_checker=False,
                 approval_route=True, decision_source=source)
    with _v1(API_KEY, stage=stage) as (client, s):
        r = client.put(f"/v1/pipeline/{DEAL}/stages/{STAGE}/advance", json={})
    assert r.status_code == 409
    assert not any("SET status       = 'completed'" in q for q in s.sql)
    assert not s.committed
