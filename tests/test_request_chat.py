"""Institution chat through the contract (1.9.1).

Templates and rendering come from the contract; rules and threads live on this side;
no borrower-database access."""

from __future__ import annotations

import inspect
import os
from contextlib import contextmanager
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import request_chat as rc  # noqa: E402
from app.deps import current_claims  # noqa: E402
from app.main import app  # noqa: E402

RID = "00000000-0000-4000-8000-000000000001"
INST = "00000000-0000-4000-8000-000000000060"
ROW = {
    "id": "m1",
    "request_id": RID,
    "sender_side": "institution",
    "body": "x",
    "kind": "structured",
    "template_code": "a.rate_final",
    "params": {},
    "created_at": "2026-10-18T09:00:00Z",
}


class _R:
    def __init__(self, row: Any = None, rows: list | None = None) -> None:
        self.row, self.rows = row, rows or []

    def one(self) -> Any:
        return self.row

    def fetchall(self) -> list:
        return self.rows


class Conn:
    def __init__(self, error: str | None = None) -> None:
        self.error, self.calls = error, []

    def execute(self, stmt: Any, params: Any = None) -> _R:
        q = str(stmt)
        self.calls.append((q, params))
        if "send_institution_message" in q:
            if self.error:
                raise DBAPIError("SELECT", {}, Exception(f"ERROR: {self.error}"))
            return _R(NS(_mapping={**ROW, "body": params["body"], "template_code": params["tpl"]}))
        if "chat_thread_state" in q:
            return _R(NS(is_open=True, is_winner=False))
        return _R(rows=[NS(_mapping={**ROW, "sender_side": "borrower"})])


@contextmanager
def client(error: str | None = None):
    conn = Conn(error)

    async def claims() -> dict:
        return {"sub": "member-1", "institution_id": INST, "user_role": "checker"}

    @contextmanager
    def svc():
        yield conn

    app.dependency_overrides[current_claims] = claims
    try:
        with patch.object(rc, "service_session", svc):
            yield TestClient(app, raise_server_exceptions=False), conn
    finally:
        app.dependency_overrides.clear()


def test_templates_come_from_the_contract():
    with client() as (c, _):
        r = c.get("/marketplace/requests/message-templates")
    codes = [t["code"] for t in r.json()]
    assert r.status_code == 200 and len(codes) == 11 and all(x.startswith("a.") for x in codes)


def test_structured_message_is_rendered_by_the_contract_not_the_caller():
    with client() as (c, conn):
        r = c.post(
            f"/marketplace/requests/{RID}/messages",
            json={
                "kind": "structured",
                "template_code": "a.processing_days",
                "params": {"days": "7"},
                "body": "ignored",
            },
        )
    assert r.status_code == 201, r.text
    sent = next(p for q, p in conn.calls if "send_institution_message" in q)
    assert sent["body"] == "Approval typically takes 7 business days."
    assert sent["iid"] == INST and sent["mid"] == "member-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "structured", "template_code": "q.insurance"},  # a borrower template
        {"kind": "structured", "template_code": "a.early_rep_fee", "params": {"percent": 99999}},
        {"kind": "structured", "template_code": "a.processing_days"},  # missing param
    ],
)
def test_contract_violations_are_422(payload):
    with client() as (c, conn):
        r = c.post(f"/marketplace/requests/{RID}/messages", json=payload)
    assert r.status_code == 422
    assert not any("send_institution_message" in q for q, _ in conn.calls)


@pytest.mark.parametrize(
    ("code", "text_"),
    [
        ("CHAT_CLOSED", "another lender's offer was accepted"),
        ("CHAT_FREE_TEXT_NOT_ALLOWED", "once the borrower accepts your offer"),
    ],
)
def test_rules_are_409(code, text_):
    with client(error=code) as (c, _):
        r = c.post(f"/marketplace/requests/{RID}/messages", json={"kind": "free", "body": "hello"})
    assert r.status_code == 409 and text_ in r.json()["detail"]


def test_thread_is_masked_and_scoped_to_the_caller():
    with client() as (c, conn):
        r = c.get(f"/marketplace/requests/{RID}/messages")
    body = r.json()
    assert body["messages"][0]["sender_type"] == "client"
    assert "member_id" not in body["messages"][0] and "sender_id" not in body["messages"][0]
    listed = next(p for q, p in conn.calls if "FROM marketplace.request_message" in q)
    assert listed["iid"] == INST


def test_module_has_no_borrower_database_access():
    src = inspect.getsource(rc)
    assert "app_service_session" not in src  # no session to the borrower database
    assert "FROM public." not in src and "INTO public." not in src
