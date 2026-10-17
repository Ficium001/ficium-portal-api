"""Products page limits + notification list through the API (not Supabase direct)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api.product_limits import ProductLimitsInput  # noqa: E402
from app.deps import current_claims  # noqa: E402
from app.main import app  # noqa: E402

INST = "31b3ee32-2864-4875-8920-cc5f27240971"
PROD = "7c2e9d3a-3333-4444-8555-b66677778888"


class _R:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    def mappings(self) -> _R:
        return self

    def all(self) -> list[dict]:
        return self.rows

    def first(self) -> dict | None:
        return self.rows[0] if self.rows else None

    def one(self) -> dict:
        return self.rows[0]


class S:
    def __init__(self, cap: dict | None) -> None:
        self.cap, self.sql, self.params, self.committed = cap, [], [], False

    def execute(self, stmt: Any, params: Any = None) -> _R:
        q = str(stmt)
        self.sql.append(q)
        self.params.append(params)
        if "FOR UPDATE OF pc" in q:
            return _R([self.cap] if self.cap else [])
        if q.lstrip().startswith("UPDATE institution.product_config"):
            return _R([{"product_id": PROD, "min_rate": params["min_rate"]}])
        if "FROM public.portal_notifications" in q:
            return _R([{"id": "n1", "title": "Deal booked"}])
        return _R()

    def commit(self) -> None:
        self.committed = True


@contextmanager
def client(role: str, cap: dict | None = None):
    s = S(cap)

    async def claims() -> dict:
        return {"sub": "u1", "institution_id": INST, "user_role": role, "email": "a@b.mu"}

    @contextmanager
    def svc():
        yield s

    app.dependency_overrides[current_claims] = claims
    try:
        with patch("app.api.product_limits.service_session", svc), \
             patch("app.api.notifications.service_session", svc):
            yield TestClient(app, raise_server_exceptions=False), s
    finally:
        app.dependency_overrides.clear()


LICENSED = {"id": "pc1", "enabled": True, "cap_min": 10000, "cap_max": 5000000}


def test_limits_need_admin() -> None:
    with client("maker", LICENSED) as (c, s):
        r = c.put(f"/institution/product-limits/{PROD}", json={"min_rate": 0.05})
    assert r.status_code == 403 and not s.committed


def test_limits_saved_and_audited() -> None:
    with client("institution_admin", LICENSED) as (c, s):
        r = c.put(f"/institution/product-limits/{PROD}",
                  json={"min_rate": 0.05, "max_rate": 0.12, "min_amount": 50000})
    assert r.status_code == 200, r.text
    assert s.committed and any("audit.event" in q for q in s.sql)
    assert any(q.lstrip().startswith("UPDATE institution.product_config") for q in s.sql)


def test_unlicensed_product_refused() -> None:
    with client("institution_admin", {**LICENSED, "enabled": False}) as (c, s):
        r = c.put(f"/institution/product-limits/{PROD}", json={"min_rate": 0.05})
    assert r.status_code == 409 and not s.committed


def test_amount_outside_platform_caps_refused() -> None:
    with client("institution_admin", LICENSED) as (c, _):
        r = c.put(f"/institution/product-limits/{PROD}", json={"max_amount": 9000000})
    assert r.status_code == 422 and "platform maximum" in r.json()["detail"]


@pytest.mark.parametrize("bad", [
    {"min_rate": 0.2, "max_rate": 0.1}, {"min_rate": 1.5}, {"min_amount": 0},
    {"min_term_months": 60, "max_term_months": 12},
])
def test_limits_validated(bad: dict) -> None:
    with pytest.raises(ValueError):
        ProductLimitsInput(**bad)


def test_notifications_list_scoped_to_institution() -> None:
    with client("maker") as (c, s):
        r = c.get("/notifications?limit=20")
    assert r.status_code == 200 and r.json()[0]["title"] == "Deal booked"
    p = next(p for q, p in zip(s.sql, s.params, strict=True) if "portal_notifications" in q)
    assert p == {"iid": INST, "lim": 20}
