"""Step 5: POST /integration/v1/acceptances, plus the shared after-acceptance side effects."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import ficium_contract as fc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import acceptance as acc  # noqa: E402
from app.api import public as pub  # noqa: E402

KEY = "accept-key-test"
BODY = json.load(open("tests/fixtures/acceptance-request.json"))
RESULT = {  # the real shape marketplace.accept_bid() returns (checked on the live schema)
    "institution_id": "f192050a-dfdd-4da7-874e-c3db88f11e41",
    "institution_name": "MCB",
    "legal_name": "MCB",
    "contact_person": "MCB Admin",
    "contact_email": "mcbadmin@mcb.mu",
    "contact_phone": "+23058610490",
    "logo_url": None,
    "pipeline_id": "ede0eaef-e443-492f-ab8b-81763af5c379",
    "rate": 0.08,
    "rate_type": "fixed",
    "amount_offered": 3000000.0,
    "term_months": 240,
}


class _R:
    def __init__(self, row: Any = None, scalar: Any = None) -> None:
        self.row, self.scalar = row, scalar

    def fetchone(self) -> Any:
        return self.row

    def scalar_one(self) -> Any:
        return self.scalar


class Fake:
    """Stands in for the service session.

    `request` is the marketplace.request row; `log` is a stored replay.
    """

    def __init__(
        self, request: Any = "default", log: Any = None, accept_error: Exception | None = None
    ) -> None:
        self.request = (
            SimpleNamespace(
                consumer_id="c0ffee00-0000-4000-8000-000000000001",
                status="bidding",
                anon_borrower_id=BODY["anon_borrower_id"],
            )
            if request == "default"
            else request
        )
        self.log, self.accept_error, self.sql, self.params = log, accept_error, [], []

    @contextmanager
    def begin_nested(self):  # noqa: ANN201
        yield

    def execute(self, stmt: Any, params: dict | None = None) -> _R:
        s = " ".join(str(stmt).split())
        self.sql.append(s)
        self.params.append(params or {})
        if "FROM integration.acceptance_log" in s:
            return _R(self.log)
        if "FROM marketplace.request r" in s:
            return _R(self.request)
        if "marketplace.accept_bid" in s:
            if self.accept_error:
                raise self.accept_error
            return _R(scalar=dict(RESULT))
        return _R()

    def called(self, needle: str) -> bool:
        return any(needle in s for s in self.sql)


@pytest.fixture()
def env(monkeypatch):
    import app.main as m

    async def _noop() -> None:
        return None

    monkeypatch.setattr(m, "init_pool", _noop)
    monkeypatch.setattr(m, "close_pool", _noop)
    monkeypatch.setattr(m.settings, "integration_acceptance_enabled", True)
    monkeypatch.setattr(m.settings, "integration_acceptance_verify_keys", f"old-key,{KEY}")
    side: list[tuple] = []

    async def _after(result, rid, bid):  # noqa: ANN001, ANN202
        side.append((result, rid, bid))

    monkeypatch.setattr(acc, "after_acceptance", _after)
    state: dict[str, Any] = {"fake": Fake()}

    @contextmanager
    def _svc():  # noqa: ANN202
        yield state["fake"]

    monkeypatch.setattr(acc, "service_session", _svc)
    with TestClient(m.app) as c:
        yield c, state, side


def call(
    c: TestClient,
    body: Any = None,
    key: str = KEY,
    idem: str | None = "idem-0001-abc",
    raw: bytes | None = None,
    sig: str | None = None,
):
    raw = raw if raw is not None else json.dumps(body if body is not None else BODY).encode()
    h = {
        "Content-Type": "application/json",
        fc.SIGNATURE_HEADER: sig if sig is not None else fc.sign(raw, key.encode()),
    }
    if idem is not None:
        h["Idempotency-Key"] = idem
    return c.post("/integration/v1/acceptances", content=raw, headers=h)


def test_off_unless_enabled(env, monkeypatch):
    c, state, side = env
    import app.main as m

    monkeypatch.setattr(m.settings, "integration_acceptance_enabled", False)
    assert call(c).status_code == 503
    monkeypatch.setattr(m.settings, "integration_acceptance_enabled", True)
    monkeypatch.setattr(m.settings, "integration_acceptance_verify_keys", "")
    assert call(c).status_code == 503
    assert state["fake"].sql == [] and side == []


def test_signature_and_its_own_key(env):
    c, state, side = env
    assert call(c, key="wrong").status_code == 401
    assert call(c, key="b2i-event-key").status_code == 401  # an EVENT key cannot accept bids
    assert (
        call(c, key="old-key").status_code == 200
    )  # rotation: the previous acceptance key still works
    assert len(side) == 1


def test_idempotency_key_and_contract_are_required(env):
    c, state, side = env
    assert call(c, idem=None).status_code == 400
    assert call(c, idem="short").status_code == 400
    bad = json.loads(json.dumps(BODY))
    bad["released_identity"]["salary"] = 90000  # outside the release allowlist
    assert call(c, body=bad).status_code == 422
    assert state["fake"].sql == [] and side == []


def test_only_the_owner_by_secret_keyed_id(env):
    c, state, side = env
    state["fake"] = Fake(
        request=SimpleNamespace(
            consumer_id="x",
            status="bidding",
            anon_borrower_id="11111111-1111-4111-8111-111111111111",
        )
    )
    r = call(c)
    assert r.status_code == 403 and r.json()["error"] == "not_the_request_owner"
    assert not state["fake"].called("accept_bid") and side == []
    state["fake"] = Fake(
        request=SimpleNamespace(consumer_id="x", status="bidding", anon_borrower_id=None)
    )
    assert (
        call(c, idem="idem-0002-abc").status_code == 403
    )  # never published through step 3: refuse


@pytest.mark.parametrize("status", ["accepted", "cancelled", "expired"])
def test_closed_requests_are_refused(env, status):
    c, state, side = env
    state["fake"] = Fake(
        request=SimpleNamespace(
            consumer_id="x", status=status, anon_borrower_id=BODY["anon_borrower_id"]
        )
    )
    r = call(c)
    assert r.status_code == 409 and r.json()["error"] == f"request_{status}"
    assert not state["fake"].called("accept_bid") and side == []


def test_success_sends_only_released_fields_and_answers_inside_the_contract(env):
    c, state, side = env
    body = json.loads(json.dumps(BODY))
    body["released_identity"] = {"full_name": "Jane Doe"}  # the borrower released ONLY the name
    r = call(c, body=body)
    assert r.status_code == 200, r.text
    fc.validate_acceptance_response(r.json())
    out = r.json()
    assert out["pipeline_id"] == RESULT["pipeline_id"] and out["deal"]["amount"] == 3000000.0
    assert out["institution"]["contact_person"] == "MCB Admin"
    i = next(n for n, s in enumerate(state["fake"].sql) if "accept_bid" in s)
    p2 = json.loads(state["fake"].params[i]["p"])
    assert (
        p2["full_name"] == "Jane Doe"
        and p2["email"] == ""
        and p2["phone"] is None
        and p2["document_number"] is None
    )
    assert (
        state["fake"].params[i]["cons"] == "c0ffee00-0000-4000-8000-000000000001"
    )  # the portal's own stored id
    assert state["fake"].called("INSERT INTO integration.acceptance_log")
    assert len(side) == 1 and side[0][0]["pipeline_id"] == RESULT["pipeline_id"]


def test_replay_returns_the_stored_answer_and_does_nothing_again(env):
    c, state, side = env
    raw = json.dumps(BODY).encode()
    import hashlib

    stored = {"bid_id": BODY["bid_id"], "stored": True}
    state["fake"] = Fake(
        log=SimpleNamespace(
            body_sha256=hashlib.sha256(raw).hexdigest(), status_code=200, response=stored
        )
    )
    r = call(c, raw=raw)
    assert r.status_code == 200 and r.json() == stored
    assert not state["fake"].called("accept_bid") and side == []


def test_same_key_with_a_different_body_is_refused(env):
    c, state, side = env
    state["fake"] = Fake(log=SimpleNamespace(body_sha256="0" * 64, status_code=200, response={}))
    r = call(c)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_reused"
    assert not state["fake"].called("accept_bid") and side == []


def test_database_refusal_is_a_409_with_no_side_effects(env):
    c, state, side = env
    state["fake"] = Fake(accept_error=DBAPIError("stmt", {}, Exception("Bid is not open")))
    r = call(c)
    assert r.status_code == 409 and r.json()["error"] == "bid_not_acceptable"
    assert state["fake"].called("INSERT INTO integration.acceptance_log") and side == []


# ── shared side effects (used by the legacy endpoint too) ───────────────────
async def test_after_acceptance_sends_real_deal_terms(monkeypatch):
    sent: list[tuple] = []
    notes: list[tuple] = []

    async def _dispatch(inst, event, payload):  # noqa: ANN001, ANN202
        sent.append((inst, event, payload))

    import app.core.webhooks as wh

    monkeypatch.setattr(wh, "dispatch_event", _dispatch)
    monkeypatch.setattr(pub, "_write_notification", lambda *a, **k: notes.append((a, k)))

    class _S:
        def commit(self) -> None:
            pass

    @contextmanager
    def _svc():  # noqa: ANN202
        yield _S()

    monkeypatch.setattr(pub, "service_session", _svc)
    await pub.after_acceptance(dict(RESULT), "r1", "b1")
    import asyncio

    await asyncio.sleep(0)
    events = {e: p for _, e, p in sent}
    assert set(events) == {"bid.accepted", "identity.revealed"}
    acc_payload = events["bid.accepted"]
    assert acc_payload["deal_amount"] == 3000000.0 and acc_payload["deal_rate"] == 0.08
    assert (
        acc_payload["deal_term_months"] == 240
        and acc_payload["pipeline_id"] == RESULT["pipeline_id"]
    )
    assert acc_payload["loan_id"] == RESULT["pipeline_id"]
    assert "full_name" not in json.dumps(events)  # no PII in webhooks
    assert len(notes) == 1


# ── the LIVE legacy endpoint still works after the refactor ─────────────────
def test_legacy_accept_bid_endpoint_still_runs_the_same_flow(monkeypatch):
    import app.main as m

    async def _noop() -> None:
        return None

    monkeypatch.setattr(m, "init_pool", _noop)
    monkeypatch.setattr(m, "close_pool", _noop)
    monkeypatch.setattr(m.settings, "app_service_secret", "svc-secret")
    consumer = "aaaaaaaa-0000-4000-8000-000000000001"
    anon = pub._anon_uuid(consumer)
    calls: list[str] = []

    class _Portal:
        def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
            s = " ".join(str(stmt).split())
            calls.append(s)
            if "SELECT r.consumer_id, r.status" in s:
                return _R(SimpleNamespace(consumer_id=anon, status="bidding"))
            if "marketplace.accept_bid" in s:
                assert json.loads(params["phase2"])["full_name"] == "Jane Doe"
                return _R(SimpleNamespace(result=dict(RESULT)))
            return _R()

    class _App:
        def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
            return _R(
                SimpleNamespace(
                    full_name="Jane Doe",
                    email="j@example.com",
                    phone=None,
                    date_of_birth=None,
                    address=None,
                    document_number=None,
                )
            )

    @contextmanager
    def _svc():  # noqa: ANN202
        yield _Portal()

    @contextmanager
    def _app():  # noqa: ANN202
        yield _App()

    side: list[tuple] = []

    async def _after(result, rid, bid):  # noqa: ANN001, ANN202
        side.append((result, rid, bid))

    monkeypatch.setattr(pub, "service_session", _svc)
    monkeypatch.setattr(pub, "app_service_session", _app)
    monkeypatch.setattr(pub, "after_acceptance", _after)
    with TestClient(m.app) as c:
        r = c.post(
            "/public/requests/r1/accept-bid",
            json={"bid_id": "b1", "consumer_id": consumer},
            headers={"X-Service-Secret": "svc-secret"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["pipeline_id"] == RESULT["pipeline_id"]
        assert (
            c.post(
                "/public/requests/r1/accept-bid",
                json={"bid_id": "b1", "consumer_id": "someone-else"},
                headers={"X-Service-Secret": "svc-secret"},
            ).status_code
            == 403
        )
    assert len(side) == 1 and side[0][1:] == ("r1", "b1")
