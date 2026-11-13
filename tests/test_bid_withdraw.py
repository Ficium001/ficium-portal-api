"""POST /marketplace/bids/{id}/withdraw: through governance (bid.withdraw), never a direct write.
The executor and the db/061 trigger do the change; this router only validates and submits."""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

INST = str(uuid.uuid4())
BID = str(uuid.uuid4())


class _R:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class Conn:
    def __init__(self, bid_status: str | None = "submitted", control: str = "four_eyes",
                 pending: bool = False, raise_on_submit: bool = False) -> None:
        self.sql: list[tuple[str, dict[str, Any]]] = []
        self.bid_status, self.control, self.pending = bid_status, control, pending
        self.raise_on_submit = raise_on_submit
        self.committed = self.rolled_back = False

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _R:
        q = " ".join(str(stmt).split())
        self.sql.append((q, params or {}))
        if "FROM marketplace.my_bids" in q:
            return _R(SimpleNamespace(status=self.bid_status) if self.bid_status else None)
        if "category = 'bid.withdraw' AND status = 'pending'" in q:
            return _R(SimpleNamespace(x=1) if self.pending else None)
        if "submit_for_approval" in q:
            if self.raise_on_submit:
                raise DBAPIError(q, params, Exception("BID_NOT_WITHDRAWABLE: bid x is accepted"))
            return _R(SimpleNamespace(id=uuid.uuid4()))
        if "SELECT status, control FROM governance.action" in q:
            status = "approved" if self.control == "single" else "pending"
            return _R(SimpleNamespace(status=status, control=self.control))
        return _R()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def direct_writes(self) -> list[str]:
        return [q for q, _ in self.sql if q.startswith("UPDATE marketplace.bid")]

    def submitted(self) -> list[dict[str, Any]]:
        return [p for q, p in self.sql if "submit_for_approval" in q]


def _client(conn: Conn, institution: str | None = INST) -> TestClient:
    async def claims() -> dict[str, Any]:
        return {"sub": str(uuid.uuid4()), "institution_id": institution, "user_role": "maker",
                "module_permissions": ["inst:bids"]}

    def tenant(claims: Any = None):  # noqa: ANN202
        yield conn

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = tenant
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    yield
    app.dependency_overrides.clear()


def _post(c: Conn, body: dict[str, Any] | None = None, **kw: Any) -> Any:
    return _client(c, **kw).post(f"/marketplace/bids/{BID}/withdraw",
                                 json={"reason": "Pricing changed"} if body is None else body)


@pytest.mark.parametrize(
    "control,expected", [("four_eyes", "pending_approval"), ("single", "withdrawn")]
)
def test_goes_through_governance(control: str, expected: str) -> None:
    c = Conn(control=control)
    r = _post(c)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == expected
    assert c.direct_writes() == []
    assert c.committed
    sub = c.submitted()
    assert len(sub) == 1 and '"reason": "Pricing changed"' in sub[0]["p"] and BID in sub[0]["p"]


@pytest.mark.parametrize("body", [{}, {"reason": "   "}, {"reason": "x" * 501}])
def test_reason_is_required_and_bounded(body: dict[str, Any]) -> None:
    c = Conn()
    assert _post(c, body).status_code == 422
    assert c.submitted() == []


@pytest.mark.parametrize("status", ["accepted", "rejected", "expired", "withdrawn"])
def test_resolved_bid_cannot_be_withdrawn(status: str) -> None:
    c = Conn(bid_status=status)
    r = _post(c)
    assert r.status_code == 409
    assert c.submitted() == []


def test_unknown_or_other_institutions_bid_is_404() -> None:
    assert _post(Conn(bid_status=None)).status_code == 404


def test_malformed_id_is_404_without_touching_the_db() -> None:
    c = Conn()
    r = _client(c).post("/marketplace/bids/not-a-uuid/withdraw", json={"reason": "x"})
    assert r.status_code == 404
    assert c.sql == []


def test_second_withdrawal_while_one_is_pending_is_409() -> None:
    c = Conn(pending=True)
    assert _post(c).status_code == 409
    assert c.submitted() == []


def test_bid_accepted_meanwhile_is_409_not_500() -> None:
    c = Conn(control="single", raise_on_submit=True)
    r = _post(c)
    assert r.status_code == 409
    assert c.rolled_back and not c.committed


def test_no_institution_context_is_403() -> None:
    assert _post(Conn(), institution=None).status_code == 403
