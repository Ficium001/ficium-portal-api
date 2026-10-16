"""
Credit approval of facilities (db/026) — what Python owns.

The state machine itself (engine outcome -> deal: approve advances, reject
declines with the comment, withdraw/expiry returns the stage to the officer,
closing a deal withdraws a running approval, maker can't vote, reject needs a
comment) is SQL and was exercised against the live schema in a rolled-back
transaction before the migration was applied. These tests cover routing,
guards and error mapping in the API.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims  # noqa: E402
from app.main import app  # noqa: E402

INST = "31b3ee32-2864-4875-8920-cc5f27240971"
USER = "4224540d-d59c-4584-8271-cb6ef24c472d"
DEAL = "5a0c7c1e-1111-4222-8333-944455556666"
TPL = "7b6e2c4c-0000-4000-8000-000000000001"
RULE = "7b6e2c4c-0000-4000-8000-000000000002"


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)
        self._mapping = kw


class _Res:
    def __init__(self, rows: list[_Row] | None = None, scalar: Any = None) -> None:
        self.rows, self._scalar = rows or [], scalar

    def fetchone(self) -> _Row | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[_Row]:
        return self.rows

    def scalar_one(self) -> Any:
        return self._scalar


class Session:
    def __init__(self, *, stage: dict[str, Any] | None = None, rules: list[_Row] | None = None,
                 close_error: str | None = None) -> None:
        self.stage, self.rules, self.close_error = stage, rules or [], close_error
        self.sql: list[str] = []
        self.params: list[Any] = []
        self.committed = False

    def execute(self, stmt: Any, params: Any = None) -> _Res:
        q = str(stmt)
        self.sql.append(q)
        self.params.append(params)
        if "FROM institution.institution" in q:          # require_module('pipeline')
            return _Res([_Row(modules=["pipeline"])])
        if "SELECT lp.id, lp.status" in q:
            return _Res([_Row(id=DEAL, status="active", current_stage_id="s1")])
        if "FROM marketplace.pipeline_stage_instance psi" in q and self.stage is not None:
            return _Res([_Row(**self.stage)])
        if "lp.deal_amount, lp.deal_rate" in q:
            return _Res([_Row(deal_amount=2_500_000, deal_rate=6.5, deal_term_months=60,
                              consumer_ref="ABCD1234", product_code="business_loan",
                              product_label="Business loan", currency="MUR",
                              borrower_name="A. Borrower")])
        if "FROM institution.doa_rule" in q:
            return _Res(self.rules)
        if "approvals_route" in q:
            return _Res([_Row(id="inst-1")])
        if "marketplace.close_deal" in q:
            if self.close_error:
                raise Exception(f"ERROR: {self.close_error}")
            return _Res(scalar={"pipeline_id": DEAL, "status": "declined", "reason": "x"})
        return _Res()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        pass


@contextmanager
def _client(session: Session):
    async def claims() -> dict[str, Any]:
        return {"sub": USER, "institution_id": INST, "user_role": "maker"}

    @contextmanager
    def svc():
        yield session

    app.dependency_overrides[current_claims] = claims
    try:
        with patch("app.deps.service_session", svc), \
             patch("app.api.pipeline.service_session", svc), \
             patch("app.api.pipeline.dispatch_event"):
            yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


ROUTED = {"id": "s1", "status": "active", "requires_maker_checker": True,
          "approval_route": True, "label": "Credit approval", "position": 4}
CATCH_ALL = _Row(id=RULE, entity_type="facility", priority=1000, conditions={}, template_id=TPL)


def test_submitting_a_credit_stage_routes_it_through_doa() -> None:
    s = Session(stage=ROUTED, rules=[CATCH_ALL])
    with _client(s) as c:
        r = c.post(f"/pipelines/{DEAL}/stages/s1/advance", json={"notes": "memo"})
    assert r.status_code == 200, r.text
    assert r.json()["approval_instance_id"] == "inst-1"
    route = s.params[next(i for i, q in enumerate(s.sql) if "approvals_route" in q)]
    assert route["tpl"] == TPL and route["rule"] == RULE and route["maker"] == USER
    assert '"amount": 2500000.0' in route["snap"]
    assert any("approval_instance_id = CAST(:inst AS uuid)" in q for q in s.sql)
    assert s.committed


def test_rules_are_loaded_for_this_institution_only() -> None:
    s = Session(stage=ROUTED, rules=[CATCH_ALL])
    with _client(s) as c:
        c.post(f"/pipelines/{DEAL}/stages/s1/advance", json={})
    i = next(i for i, q in enumerate(s.sql) if "FROM institution.doa_rule" in q)
    assert "institution_id = :iid" in s.sql[i] and s.params[i]["iid"] == INST


def test_no_matching_rule_is_a_clear_409() -> None:
    s = Session(stage=ROUTED, rules=[])
    with _client(s) as c:
        r = c.post(f"/pipelines/{DEAL}/stages/s1/advance", json={})
    assert r.status_code == 409
    assert "credit approval rule" in r.json()["detail"]


def test_ordinary_stage_is_unchanged() -> None:
    s = Session(stage={**ROUTED, "approval_route": False, "requires_maker_checker": False},
                rules=[CATCH_ALL])
    with _client(s) as c, patch("app.api.pipeline._complete_stage",
                                 return_value={"status": "advanced"}) as done:
        r = c.post(f"/pipelines/{DEAL}/stages/s1/advance", json={})
    assert r.status_code == 200 and done.called
    assert not any("approvals_route" in q for q in s.sql)


def test_checker_cannot_click_through_a_credit_decision() -> None:
    s = Session(stage={"id": "s1", "status": "awaiting_approval", "submitted_by": "other",
                       "position": 4, "approval_instance_id": "inst-1"})
    with _client(s) as c:
        r = c.post(f"/pipelines/{DEAL}/stages/s1/approve", json={})
    assert r.status_code == 409
    assert "Inbox" in r.json()["detail"]


def test_close_deal_maps_rule_errors() -> None:
    for code, status in {"DEAL_REASON_REQUIRED": 422, "DEAL_NOT_ACTIVE": 409,
                         "DEAL_NOT_FOUND": 404, "DEAL_OUTCOME_INVALID": 422}.items():
        with _client(Session(close_error=code)) as c:
            r = c.post(f"/pipelines/{DEAL}/close", json={"outcome": "declined", "reason": "x"})
        assert r.status_code == status, code


def test_close_deal_audits() -> None:
    s = Session()
    with _client(s) as c:
        r = c.post(f"/pipelines/{DEAL}/close",
                   json={"outcome": "withdrawn", "reason": "Borrower took another offer"})
    assert r.status_code == 200, r.text
    i = next(i for i, q in enumerate(s.sql) if "audit.event" in q)
    assert s.params[i]["action"] == "deal.withdrawn"
    assert s.committed


def test_deal_closed_is_a_webhook_event() -> None:
    from app.api.webhooks import _VALID_EVENTS
    assert "deal.closed" in _VALID_EVENTS
