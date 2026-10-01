"""Contract v1 plumbing: inbound endpoint, dispatcher, outbox. No database:
the SQL side is exercised by the DB smoke test recorded in db/011_integration.sql."""

from __future__ import annotations

import json
from typing import Any

import ficium_contract as fc
import httpx
import pytest
from fastapi.testclient import TestClient

B2I = "b2i-test-key"
I2B = "i2b-test-key"


def ping(
    source: str = "borrower", seq: int = 1, event_id: str = "evt_0123456789abcdef"
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "ping",
        "version": 1,
        "source": source,
        "occurred_at": "2026-10-01T09:00:00Z",
        "aggregate_id": "ping",
        "sequence": seq,
        "data": {},
    }


@pytest.fixture()
def client(monkeypatch):
    import app.main as m
    from app.api import integration as api

    async def _noop() -> None:
        return None

    monkeypatch.setattr(m, "init_pool", _noop)
    monkeypatch.setattr(m, "close_pool", _noop)
    monkeypatch.setattr(m.settings, "integration_b2i_verify_keys", f"old-key,{B2I}")
    monkeypatch.setattr(m.settings, "integration_i2b_signing_key", "")  # no dispatcher in tests
    recorded: list[dict[str, Any]] = []

    def fake_record(env: dict[str, Any]) -> str:
        recorded.append(env)
        return "apply"

    monkeypatch.setattr(api, "_record_and_apply", fake_record)
    with TestClient(m.app) as c:
        c.recorded = recorded  # type: ignore[attr-defined]
        yield c


def post(
    client: TestClient,
    env: Any,
    key: str = B2I,
    raw: bytes | None = None,
    header: str | None = None,
):
    body = raw if raw is not None else json.dumps(env).encode()
    h = header if header is not None else fc.sign(body, key.encode())
    return client.post(
        "/integration/v1/events",
        content=body,
        headers={"Content-Type": "application/json", fc.SIGNATURE_HEADER: h},
    )


def test_valid_ping_is_recorded(client):
    r = post(client, ping())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "apply"
    assert len(client.recorded) == 1


def test_rotated_old_key_still_accepted(client):
    assert post(client, ping(), key="old-key").status_code == 200


def test_bad_signature_rejected_and_not_recorded(client):
    assert post(client, ping(), key="wrong").status_code == 401
    assert post(client, ping(), header="garbage").status_code == 401
    body = json.dumps(ping()).encode()
    tampered = body.replace(b'"sequence": 1', b'"sequence": 9')
    assert post(client, None, raw=tampered, header=fc.sign(body, B2I.encode())).status_code == 401
    assert client.recorded == []


def test_contract_violation_rejected(client):
    bad = ping()
    bad["version"] = 2
    r = post(client, bad)
    assert r.status_code == 422
    assert client.recorded == []


def test_events_from_institution_side_rejected(client):
    assert post(client, ping(source="institution")).status_code == 403
    assert client.recorded == []


def test_unhandled_type_not_recorded_so_sender_retries(client):
    env = json.load(open("tests/fixtures/request-published.json"))
    r = post(client, env)
    assert r.status_code == 501
    assert client.recorded == []


def test_disabled_without_keys(client, monkeypatch):
    import app.main as m

    monkeypatch.setattr(m.settings, "integration_b2i_verify_keys", "")
    assert post(client, ping()).status_code == 503


# ── dispatcher ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("status", "expected"), [(200, "delivered"), (503, "failed"), (401, "failed")]
)
async def test_dispatcher_signs_sends_and_marks(monkeypatch, status, expected):
    from app.integration import dispatcher as d

    env = ping(source="institution")
    marks: list[tuple[str, bool, str]] = []
    monkeypatch.setattr(d.settings, "integration_i2b_signing_key", I2B)
    monkeypatch.setattr(d.settings, "integration_peer_url", "https://borrower.test/api/integration")
    monkeypatch.setattr(d, "_claim", lambda n: [(env["id"], env)])

    def fake_mark(event_id: str, ok: bool, err: str) -> str:
        marks.append((event_id, ok, err))
        return "delivered" if ok else "pending"

    monkeypatch.setattr(d, "_mark", fake_mark)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await d.dispatch_once(c)

    assert len(seen) == 1
    fc.verify(seen[0].headers[fc.SIGNATURE_HEADER], seen[0].content, [I2B.encode()])
    assert json.loads(seen[0].content) == env
    assert marks[0][1] is (expected == "delivered")


async def test_dispatcher_network_error_marks_failed(monkeypatch):
    from app.integration import dispatcher as d

    env = ping(source="institution")
    marks: list[bool] = []
    monkeypatch.setattr(d.settings, "integration_i2b_signing_key", I2B)
    monkeypatch.setattr(d.settings, "integration_peer_url", "https://borrower.test/api/integration")
    monkeypatch.setattr(d, "_claim", lambda n: [(env["id"], env)])
    monkeypatch.setattr(d, "_mark", lambda i, ok, e: marks.append(ok) or "pending")

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        await d.dispatch_once(c)
    assert marks == [False]


# ── outbox ─────────────────────────────────────────────────────────────────
class FakeResult:
    def __init__(self, v: Any) -> None:
        self.v = v

    def scalar_one(self) -> Any:
        return self.v


class FakeSession:
    def __init__(self, env: dict[str, Any]) -> None:
        self.env = env

    def execute(self, *_a: Any, **_k: Any) -> FakeResult:
        return FakeResult(self.env)


def test_enqueue_validates_what_the_db_built():
    from app.integration.outbox import enqueue

    ok = ping(source="institution")
    assert enqueue(FakeSession(ok), "ping", "ping", {}) == ok  # type: ignore[arg-type]
    with pytest.raises(fc.ContractError):
        enqueue(FakeSession(ping(source="martian")), "ping", "ping", {})  # type: ignore[arg-type]
