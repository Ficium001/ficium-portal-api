# =============================================================================
# Unit tests — app/core/entitlements.py
#
# DB-free: exercises the pure evaluation function, the TTL cache, and the
# require_module dependency wired into a minimal FastAPI app with the claims
# dependency overridden. Integration (real Postgres + RLS) is covered by the
# invariant suite per tests/INTEGRATION_SETUP.md.
# =============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import entitlements as ent
from app.core.entitlements import (
    Entitlement,
    EntitlementError,
    evaluate,
    invalidate_cache,
    require_module,
)
from app.deps import api_or_jwt_claims

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def make_ent(**overrides: Any) -> Entitlement:
    base: dict[str, Any] = dict(
        institution_id="11111111-1111-1111-1111-111111111111",
        module_code="AUTOBID",
        status="active",
        plan_tier="professional",
        valid_from=NOW - timedelta(days=10),
        valid_to=None,
        limits={"max_active_rules": 50},
    )
    base.update(overrides)
    return Entitlement(**base)


# ---------------------------------------------------------------------------
# evaluate() — pure decision logic
# ---------------------------------------------------------------------------

def test_evaluate_active_passes() -> None:
    row = make_ent()
    assert evaluate(row, NOW) is row


def test_evaluate_trial_passes() -> None:
    assert evaluate(make_ent(status="trial"), NOW).status == "trial"


def test_evaluate_missing_raises_not_licensed() -> None:
    with pytest.raises(EntitlementError) as exc:
        evaluate(None, NOW)
    assert exc.value.code == "module_not_licensed"


@pytest.mark.parametrize("status", ["suspended", "expired"])
def test_evaluate_bad_status_raises_suspended(status: str) -> None:
    with pytest.raises(EntitlementError) as exc:
        evaluate(make_ent(status=status), NOW)
    assert exc.value.code == "module_suspended"


def test_evaluate_not_yet_valid() -> None:
    with pytest.raises(EntitlementError) as exc:
        evaluate(make_ent(valid_from=NOW + timedelta(days=1)), NOW)
    assert exc.value.code == "module_not_yet_valid"


def test_evaluate_expired_window() -> None:
    with pytest.raises(EntitlementError) as exc:
        evaluate(make_ent(valid_to=NOW - timedelta(seconds=1)), NOW)
    assert exc.value.code == "module_expired"


def test_evaluate_valid_to_boundary_is_exclusive() -> None:
    # valid_to exactly now => expired (<= comparison)
    with pytest.raises(EntitlementError):
        evaluate(make_ent(valid_to=NOW), NOW)
    # one second in the future => still valid
    assert evaluate(make_ent(valid_to=NOW + timedelta(seconds=1)), NOW)


# ---------------------------------------------------------------------------
# TTL cache behaviour (resolve_entitlement with _load_entitlement stubbed)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    invalidate_cache()


def test_resolve_uses_cache_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_load(conn: Any, inst: str, mod: str) -> Entitlement:
        calls["n"] += 1
        return make_ent(institution_id=inst, module_code=mod)

    class FakeCtx:
        def __enter__(self) -> Any:
            return object()

        def __exit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(ent, "_load_entitlement", fake_load)
    monkeypatch.setattr(ent, "service_session", lambda: FakeCtx())

    inst = "22222222-2222-2222-2222-222222222222"
    ent.resolve_entitlement(inst, "AUTOBID")
    ent.resolve_entitlement(inst, "AUTOBID")
    assert calls["n"] == 1  # second hit served from cache

    invalidate_cache(inst)
    ent.resolve_entitlement(inst, "AUTOBID")
    assert calls["n"] == 2  # invalidation forces reload


def test_negative_result_is_cached_too(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_load(conn: Any, inst: str, mod: str) -> None:
        calls["n"] += 1
        return None

    class FakeCtx:
        def __enter__(self) -> Any:
            return object()

        def __exit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(ent, "_load_entitlement", fake_load)
    monkeypatch.setattr(ent, "service_session", lambda: FakeCtx())

    inst = "33333333-3333-3333-3333-333333333333"
    for _ in range(3):
        with pytest.raises(EntitlementError):
            ent.resolve_entitlement(inst, "SERVICING")
    assert calls["n"] == 1  # unlicensed lookups don't hammer the DB


# ---------------------------------------------------------------------------
# require_module — HTTP contract
# ---------------------------------------------------------------------------

def build_app(claims: dict[str, Any]) -> TestClient:
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(require_module("AUTOBID"))])
    def guarded() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[api_or_jwt_claims] = lambda: claims
    return TestClient(app)


def test_require_module_allows_entitled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ent, "resolve_entitlement", lambda i, m: make_ent(institution_id=i, module_code=m)
    )
    client = build_app({"institution_id": "44444444-4444-4444-4444-444444444444"})
    r = client.get("/guarded")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_require_module_403_with_machine_readable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(i: str, m: str) -> Entitlement:
        raise EntitlementError("module_not_licensed", "nope")

    monkeypatch.setattr(ent, "resolve_entitlement", deny)
    client = build_app({"institution_id": "44444444-4444-4444-4444-444444444444"})
    r = client.get("/guarded")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "module_not_licensed"
    assert r.json()["detail"]["module"] == "AUTOBID"


def test_require_module_403_without_institution_context() -> None:
    client = build_app({"sub": "someone", "auth_method": "jwt"})
    r = client.get("/guarded")
    assert r.status_code == 403
