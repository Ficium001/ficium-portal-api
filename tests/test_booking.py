"""
Deal booking hand-off: HTTP layer + service.

The business rules (deal must be complete, amount <= deal, one booking per
deal, unique core reference, commission re-based only while pending) live in
marketplace.record_loan_booking() and were verified against the live schema
in a rolled-back transaction before db/025 was applied. These tests cover what
Python owns: permissions, scope checks, error mapping, side effects, wiring.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.core import booking as svc  # noqa: E402
from app.deps import api_or_jwt_claims, current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

INST = "31b3ee32-2864-4875-8920-cc5f27240971"
USER = "4224540d-d59c-4584-8271-cb6ef24c472d"
DEAL = "5a0c7c1e-1111-4222-8333-944455556666"

BODY = {
    "core_reference": "LN-2026-0001", "booked_on": "2026-10-15", "disbursed_amount": "950000.00",
}
BOOKING = {"pipeline_id": DEAL, "core_reference": "LN-2026-0001", "booked_on": "2026-10-15",
           "disbursed_amount": 950000.0, "disbursed_on": None}


# ── fakes ────────────────────────────────────────────────────────────────────
class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Result:
    def __init__(self, scalar: Any = None, first: Any = None) -> None:
        self._scalar, self._first = scalar, first

    def scalar_one(self) -> Any:
        return self._scalar

    def scalar(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._first

    def fetchone(self) -> Any:
        return self._first

    def mappings(self) -> _Result:
        return self


class FakeSession:
    """Answers the booking function call; records every statement."""

    def __init__(self, fn_result: Any = None, fn_error: Exception | None = None,
                 modules: list[str] | None = None) -> None:
        self.fn_result, self.fn_error, self.modules = fn_result, fn_error, modules
        self.fee_inputs: Any = {"monthly_amount": False, "term_months": False}
        self.params: Any = None
        self.sql: list[str] = []
        self.committed = self.rolled_back = False

    def execute(self, stmt: Any, params: Any = None) -> _Result:
        q = str(stmt)
        self.sql.append(q)
        if "record_loan_booking" in q or "record_booking_closure" in q:
            self.params = params
            if self.fn_error:
                raise self.fn_error
            return _Result(scalar=self.fn_result)
        if "booking_fee_inputs" in q:
            return _Result(scalar=self.fee_inputs)
        if "FROM institution.institution" in q:  # require_module("pipeline") licence check
            return _Result(first=_Row(modules=["pipeline"]))
        if "modules" in q:  # member's group permissions
            return _Result(first=_Row(modules=self.modules or []))
        return _Result()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _db_error(code: str) -> DBAPIError:
    return DBAPIError("SELECT ...", {}, Exception(f"ERROR: {code}\nCONTEXT: PL/pgSQL"))


# ── service ──────────────────────────────────────────────────────────────────
def test_error_codes_map_to_http() -> None:
    cases = {
        "BOOKING_DEAL_NOT_COMPLETE": 409, "BOOKING_ALREADY_RECORDED": 409,
        "BOOKING_REFERENCE_IN_USE": 409, "BOOKING_AMOUNT_EXCEEDS_DEAL": 422,
        "BOOKING_DEAL_NOT_FOUND": 404, "BOOKING_NOT_FOUND": 404,
    }
    for code, status in cases.items():
        mapped = svc._booking_error(_db_error(code))
        assert mapped is not None and mapped.status_code == status, code


def test_fee_model_error_codes_map_to_http() -> None:
    cases = {
        "FEE_INPUT_MISSING:monthly_amount": (422, "monthly amount"),
        "FEE_INPUT_MISSING:term_months": (422, "term"),
        "BOOKING_CLOSED": (409, "closed"), "BOOKING_ALREADY_CLOSED": (409, "already"),
        "BOOKING_CLOSURE_DATE_INVALID": (422, "closing date"),
        "BOOKING_CLOSURE_REASON_INVALID": (422, "reason"),
        "BOOKING_FEE_CURRENCY_MISMATCH": (409, "currency"),
        "BOOKING_NO_COMMISSION_RULE": (409, "commission"),
        "BOOKING_MONTHLY_INVALID": (422, "monthly"), "BOOKING_TERM_INVALID": (422, "term"),
    }
    for code, (status, word) in cases.items():
        mapped = svc._booking_error(_db_error(code))
        assert mapped is not None and mapped.status_code == status, code
        assert word in str(mapped.detail).lower(), code


def test_unknown_db_error_is_not_swallowed() -> None:
    assert svc._booking_error(_db_error("deadlock detected")) is None


@pytest.mark.parametrize(("modules", "role", "ok"), [
    (["inst:booking"], "maker", True),
    (["*"], "maker", True),
    (["inst:pipeline"], "maker", False),
    ([], "institution_super_admin", True),
    (None, "maker", False),
])
def test_permission(modules: list[str] | None, role: str, ok: bool) -> None:
    assert svc.has_booking_permission(modules, role) is ok


def _record(session: FakeSession, **over: Any) -> tuple[dict[str, Any], BackgroundTasks]:
    bg = BackgroundTasks()
    kwargs: dict[str, Any] = dict(
        institution_id=INST, pipeline_id=DEAL, body=svc.BookingInput(**BODY), source="portal",
        actor_id=USER, actor_email="a@b.mu", api_key_id=None, correction=False, background=bg,
    )
    kwargs.update(over)
    return svc.record_booking(session, **kwargs), bg  # type: ignore[arg-type]


def test_record_writes_audit_and_notification_then_queues_webhook() -> None:
    s = FakeSession(fn_result={"booking": BOOKING, "previous": None,
                               "commission_status": "pending"})
    booking, bg = _record(s)
    assert booking["core_reference"] == "LN-2026-0001"
    assert s.committed
    assert any("portal_notifications" in q for q in s.sql)
    assert any("audit.event" in q for q in s.sql)
    assert len(bg.tasks) == 1
    assert bg.tasks[0].args[1] == "deal.booked"


def test_correction_fires_correction_event() -> None:
    s = FakeSession(fn_result={"booking": BOOKING, "previous": BOOKING,
                               "commission_status": "pending"})
    _, bg = _record(s, correction=True)
    assert bg.tasks[0].args[1] == "deal.booking_corrected"


def test_fee_inputs_reach_the_booking_function() -> None:
    s = FakeSession(fn_result={"booking": BOOKING, "previous": None,
                               "commission_status": "pending"})
    body = svc.BookingInput(**{**BODY, "monthly_amount": "500.00", "term_months": 24})
    _, bg = _record(s, body=body)
    assert str(s.params["monthly"]) == "500.00" and s.params["term"] == 24
    assert any("CAST(:monthly AS numeric), CAST(:term AS integer)" in q for q in s.sql)


def test_fee_inputs_default_to_none() -> None:
    s = FakeSession(fn_result={"booking": BOOKING, "previous": None,
                               "commission_status": "pending"})
    _record(s)
    assert s.params["monthly"] is None and s.params["term"] is None


def _close(session: FakeSession) -> tuple[dict[str, Any], BackgroundTasks]:
    bg = BackgroundTasks()
    out = svc.record_closure(
        session, institution_id=INST, pipeline_id=DEAL,  # type: ignore[arg-type]
        body=svc.ClosureInput(closed_on="2026-11-20", reason="client stopped the plan"),
        source="api", actor_id=None, actor_email=None, api_key_id="k-1", background=bg,
    )
    return out, bg


CLOSED = {**BOOKING, "closed_on": "2026-11-20", "closure_reason": "client stopped the plan"}


def test_closure_audits_the_clawback_but_does_not_return_it() -> None:
    clawback = {"amount": -250.0, "currency": "MUR", "effective_on": "2026-11-20"}
    s = FakeSession(fn_result={"booking": CLOSED, "clawback": clawback})
    out, bg = _close(s)
    assert "clawback" not in out and out["closed_on"] == "2026-11-20"
    audit_params = s.params  # last captured is the function call; check the SQL text instead
    assert audit_params["source"] == "api" and audit_params["key"] == "k-1"
    assert any("'deal.booking_closed'" in q for q in s.sql)
    assert any("'booking_closed'" in q and "portal_notifications" in q for q in s.sql)
    assert s.committed
    assert bg.tasks[0].args[1] == "deal.booking_closed"
    assert "clawback" not in bg.tasks[0].args[2]


def test_closure_rule_violation_rolls_back() -> None:
    s = FakeSession(fn_error=_db_error("BOOKING_ALREADY_CLOSED"))
    with pytest.raises(HTTPException) as e:
        _close(s)
    assert e.value.status_code == 409 and s.rolled_back and not s.committed


@pytest.mark.parametrize("bad", [
    {"closed_on": "2026-11-20", "reason": "x"},
    {"closed_on": "2026-11-20", "reason": "y" * 501},
    {"reason": "client stopped the plan"},
])
def test_closure_body_is_validated(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        svc.ClosureInput(**bad)


@pytest.mark.parametrize("bad", [
    {**BODY, "monthly_amount": "-1"},
    {**BODY, "term_months": 0},
    {**BODY, "term_months": 601},
    {**BODY, "monthly_amount": "1.001"},
])
def test_booking_fee_inputs_are_validated(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        svc.BookingInput(**bad)


def test_rule_violation_rolls_back_and_maps() -> None:
    s = FakeSession(fn_error=_db_error("BOOKING_DEAL_NOT_COMPLETE"))
    with pytest.raises(HTTPException) as e:
        _record(s)
    assert e.value.status_code == 409
    assert s.rolled_back and not s.committed


# ── portal endpoints ─────────────────────────────────────────────────────────
@contextmanager
def _portal(modules: list[str], fn_result: Any = None):
    session = FakeSession(fn_result=fn_result, modules=modules)

    async def claims() -> dict[str, Any]:
        return {"sub": USER, "institution_id": INST, "user_role": "maker", "email": "a@b.mu"}

    def conn(claims: Any = None):
        yield session

    @contextmanager
    def service():
        yield session

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = conn
    try:
        with patch("app.deps.service_session", service), \
             patch("app.api.booking.service_session", service), \
             patch("app.core.booking.dispatch_event"):
            yield TestClient(app, raise_server_exceptions=False), session
    finally:
        app.dependency_overrides.clear()


def test_portal_requires_booking_permission() -> None:
    with _portal(["inst:pipeline"]) as (client, _):
        r = client.post(f"/pipelines/{DEAL}/booking", json=BODY)
    assert r.status_code == 403


def test_portal_records_booking() -> None:
    result = {"booking": BOOKING, "previous": None, "commission_status": "pending"}
    with _portal(["inst:pipeline", "inst:booking"], fn_result=result) as (client, s):
        r = client.post(f"/pipelines/{DEAL}/booking", json=BODY)
    assert r.status_code == 201, r.text
    assert r.json()["core_reference"] == "LN-2026-0001"
    assert any("record_loan_booking" in q for q in s.sql)


@pytest.mark.parametrize("bad", [
    {**BODY, "disbursed_amount": "0"},
    {**BODY, "core_reference": ""},
    {**BODY, "core_reference": "x" * 65},
    {k: v for k, v in BODY.items() if k != "booked_on"},
])
def test_portal_validates_body(bad: dict[str, Any]) -> None:
    with _portal(["inst:booking"]) as (client, _):
        r = client.post(f"/pipelines/{DEAL}/booking", json=bad)
    assert r.status_code == 422


def test_portal_requirements_say_which_fields_to_send() -> None:
    with _portal(["inst:pipeline"]) as (client, s):
        s.fee_inputs = {"monthly_amount": True, "term_months": False}
        r = client.get(f"/pipelines/{DEAL}/booking/requirements")
    assert r.status_code == 200, r.text
    assert r.json()["requires"] == {"monthly_amount": True, "term_months": False}
    assert "rate" not in r.text


def test_portal_requirements_404_for_another_banks_deal() -> None:
    with _portal(["inst:pipeline"]) as (client, s):
        s.fee_inputs = None
        r = client.get(f"/pipelines/{DEAL}/booking/requirements")
    assert r.status_code == 404


def test_portal_closure_needs_booking_permission() -> None:
    with _portal(["inst:pipeline"]) as (client, _):
        r = client.post(f"/pipelines/{DEAL}/booking/closure",
                        json={"closed_on": "2026-11-20", "reason": "client stopped the plan"})
    assert r.status_code == 403


def test_portal_records_closure() -> None:
    with _portal(["inst:booking"], fn_result={"booking": CLOSED, "clawback": None}) as (client, s):
        r = client.post(f"/pipelines/{DEAL}/booking/closure",
                        json={"closed_on": "2026-11-20", "reason": "client stopped the plan"})
    assert r.status_code == 200, r.text
    assert r.json()["closed_on"] == "2026-11-20"
    assert s.params["source"] == "portal" and s.params["actor"] == USER


# ── v1 (bank systems) ────────────────────────────────────────────────────────
@contextmanager
def _v1(claims: dict[str, Any], modules: list[str] | None = None, fn_result: Any = None):
    session = FakeSession(fn_result=fn_result, modules=modules)

    async def dep() -> dict[str, Any]:
        return claims

    @contextmanager
    def service():
        yield session

    app.dependency_overrides[api_or_jwt_claims] = dep
    try:
        with patch("app.api.v1.marketplace._svc", service), \
             patch("app.core.booking.dispatch_event"):
            yield TestClient(app, raise_server_exceptions=False), session
    finally:
        app.dependency_overrides.clear()


API_KEY = {
    "auth_method": "api_key", "institution_id": INST, "key_id": "k-1", "scopes": ["pipeline:write"],
}


def test_v1_requires_pipeline_write_scope() -> None:
    with _v1({**API_KEY, "scopes": ["bids:read"]}) as (client, _):
        r = client.post(f"/v1/pipeline/{DEAL}/booking", json=BODY)
    assert r.status_code == 403


def test_v1_api_key_books_as_api_source() -> None:
    result = {"booking": BOOKING, "previous": None, "commission_status": "pending"}
    with _v1(API_KEY, fn_result=result) as (client, s), \
         patch("app.api.v1.marketplace.record_booking", wraps=svc.record_booking) as spy:
        r = client.post(f"/v1/pipeline/{DEAL}/booking", json=BODY)
    assert r.status_code == 201, r.text
    assert r.json()["loan_id"] == DEAL
    kw = spy.call_args.kwargs
    assert kw["source"] == "api" and kw["api_key_id"] == "k-1" and kw["actor_id"] is None


def test_v1_api_key_closes_as_api_source() -> None:
    with _v1(API_KEY, fn_result={"booking": CLOSED, "clawback": None}) as (client, s):
        r = client.post(f"/v1/pipeline/{DEAL}/booking/closure",
                        json={"closed_on": "2026-11-20", "reason": "client stopped the plan"})
    assert r.status_code == 200, r.text
    assert r.json()["loan_id"] == DEAL
    assert s.params["source"] == "api" and s.params["key"] == "k-1" and s.params["actor"] is None


def test_v1_closure_requires_pipeline_write_scope() -> None:
    with _v1({**API_KEY, "scopes": ["bids:read"]}) as (client, _):
        r = client.post(f"/v1/pipeline/{DEAL}/booking/closure",
                        json={"closed_on": "2026-11-20", "reason": "client stopped the plan"})
    assert r.status_code == 403


def test_v1_requirements() -> None:
    with _v1(API_KEY) as (client, s):
        s.fee_inputs = {"monthly_amount": False, "term_months": True}
        r = client.get(f"/v1/pipeline/{DEAL}/booking/requirements")
    assert r.status_code == 200, r.text
    assert r.json() == {"loan_id": DEAL, "requires": {"monthly_amount": False, "term_months": True}}


def test_v1_jwt_user_needs_booking_permission() -> None:
    jwt = {"auth_method": "jwt", "institution_id": INST, "sub": USER, "user_role": "maker"}
    with _v1(jwt, modules=["inst:pipeline"]) as (client, _):
        r = client.post(f"/v1/pipeline/{DEAL}/booking", json=BODY)
    assert r.status_code == 403


# ── v1 stage advance (was broken: compared against non-existent statuses) ────
class _AdvanceSession(FakeSession):
    def __init__(self, stage_status: str, maker_checker: bool, has_next: bool) -> None:
        super().__init__()
        self.stage_status, self.mc, self.has_next = stage_status, maker_checker, has_next

    def execute(self, stmt: Any, params: Any = None) -> _Result:
        q = str(stmt)
        self.sql.append(q)
        if "FROM marketplace.loan_pipeline" in q and "SELECT id, status" in q:
            return _Result(first=_Row(id=DEAL, status="active"))
        if "FROM marketplace.pipeline_stage_instance psi" in q:
            return _Result(first=_Row(id="s1", status=self.stage_status, position=1,
                                      stage_key="kyc", borrower_label="KYC",
                                      requires_maker_checker=self.mc,
                                      approval_route=False, decision_source="internal"))
        if "RETURNING id, position" in q:
            return _Result(first=_Row(id="s2", position=2) if self.has_next else None)
        return _Result()


@contextmanager
def _advance(session: _AdvanceSession):
    async def dep() -> dict[str, Any]:
        return API_KEY

    @contextmanager
    def service():
        yield session

    app.dependency_overrides[api_or_jwt_claims] = dep
    try:
        with patch("app.api.v1.marketplace._svc", service), \
             patch("app.api.v1.marketplace.dispatch_event"):
            yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def test_v1_advance_completes_active_stage_and_activates_next() -> None:
    s = _AdvanceSession("active", maker_checker=False, has_next=True)
    with _advance(s) as client:
        r = client.put(f"/v1/pipeline/{DEAL}/stages/s1/advance", json={})
    assert r.status_code == 200, r.text
    assert r.json()["next_stage_id"] == "s2"
    assert any("SET status     = 'active'" in q for q in s.sql)
    assert any("current_stage_id" in q for q in s.sql)


def test_v1_advance_sends_maker_checker_stage_for_approval() -> None:
    s = _AdvanceSession("active", maker_checker=True, has_next=True)
    with _advance(s) as client:
        r = client.put(f"/v1/pipeline/{DEAL}/stages/s1/advance", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_approval"


def test_v1_advance_last_stage_completes_deal() -> None:
    s = _AdvanceSession("active", maker_checker=False, has_next=False)
    with _advance(s) as client:
        r = client.put(f"/v1/pipeline/{DEAL}/stages/s1/advance", json={})
    assert r.json()["pipeline_completed"] is True


def test_v1_advance_rejects_non_active_stage() -> None:
    s = _AdvanceSession("pending", maker_checker=False, has_next=True)
    with _advance(s) as client:
        r = client.put(f"/v1/pipeline/{DEAL}/stages/s1/advance", json={})
    assert r.status_code == 409


def test_webhook_events_registered() -> None:
    from app.api.webhooks import _VALID_EVENTS
    assert {"deal.booked", "deal.booking_corrected", "deal.booking_closed"} <= _VALID_EVENTS


_ = SimpleNamespace  # keep import for readers extending fakes
