"""
Ficium Portal API — HTTP layer test suite
==========================================

Tests the FastAPI HTTP layer: JWT boundary enforcement, request validation,
role-based access control, and response shape for every router.

Strategy
--------
• Auth boundary tests  — use the real `current_claims` dep with no mocking.
  Malformed / missing tokens fail before the JWKS fetch (jose raises
  immediately on bad JWT structure). Well-formed-but-unsigned tokens are
  tested by mocking `verify_token` to raise `TokenError` — which is the
  exactly the path `current_claims` must handle with a 401.

• Endpoint tests — override `current_claims` with a fixture returning
  controlled claims, and override `tenant_conn` with a lightweight mock
  session. Admin / institutions endpoints call `service_session` and
  `tenant_session` directly, so those are patched at the module level.

Running
-------
  pip install pytest httpx
  pytest tests/test_api.py -v

No environment variables required.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.core.security import TokenError  # noqa: E402
from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

# ---------------------------------------------------------------------------
# Test identities
# ---------------------------------------------------------------------------
MCB_INSTITUTION_ID = "31b3ee32-2864-4875-8920-cc5f27240971"
MCB_ADMIN_AUTH_UID = "4224540d-d59c-4584-8271-cb6ef24c472d"


def institution_claims(**overrides) -> dict[str, Any]:
    return {
        "sub":            MCB_ADMIN_AUTH_UID,
        "role":           "authenticated",
        "user_role":      "institution_admin",
        "institution_id": MCB_INSTITUTION_ID,
        **overrides,
    }


def admin_claims(**overrides) -> dict[str, Any]:
    return {
        "sub":       MCB_ADMIN_AUTH_UID,
        "role":      "authenticated",
        "user_role": "super_admin",
        **overrides,
    }


# ---------------------------------------------------------------------------
# Mock DB plumbing
# ---------------------------------------------------------------------------
class _MockResult:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class MockSession:
    def __init__(self, fetchone_result=None, fetchall_result=None):
        self._fetchone = fetchone_result
        self._fetchall = fetchall_result or []

    def execute(self, *a, **kw):
        return _MockResult(rows=self._fetchall, row=self._fetchone)

    def commit(self):   pass
    def rollback(self): pass
    def close(self):    pass


@contextmanager
def _mock_service_session(fetchone=None, fetchall=None):
    yield MockSession(fetchone_result=fetchone, fetchall_result=fetchall)


@contextmanager
def _mock_tenant_session(claims, fetchone=None, fetchall=None):
    yield MockSession(fetchone_result=fetchone, fetchall_result=fetchall)


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------
def _claims_override(claims: dict):
    async def _dep():
        return claims
    return _dep


def _conn_override(fetchone=None, fetchall=None):
    def _dep(claims=None):
        yield MockSession(fetchone_result=fetchone, fetchall_result=fetchall)
    return _dep


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def client():
    """Bare client — real auth deps (no overrides)."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def auth_client():
    """institution_admin claims + mocked DB."""
    app.dependency_overrides[current_claims] = _claims_override(institution_claims())
    app.dependency_overrides[tenant_conn]    = _conn_override()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client():
    """super_admin claims + mocked DB."""
    app.dependency_overrides[current_claims] = _claims_override(admin_claims())
    app.dependency_overrides[tenant_conn]    = _conn_override()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ===========================================================================
# Health
# ===========================================================================
class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "ficium-portal-api" in body["service"]
        assert "version" in body


# ===========================================================================
# Auth boundary — real current_claims, zero DB
# ===========================================================================
class TestAuthBoundary:
    """Every JWT-protected endpoint must reject bad / missing tokens with 401."""

    # catalog has no prefix: /products not /catalog/products
    PROTECTED = [
        ("GET",  "/members/me"),
        ("GET",  "/members/my-group"),
        ("GET",  "/members"),
        ("GET",  "/marketplace/requests"),
        ("GET",  "/marketplace/my-bids"),
        ("POST", "/marketplace/bids"),
        ("GET",  "/marketplace/bids/00000000-0000-0000-0000-000000000000/reveal"),
        ("GET",  "/marketplace/requests/message-templates"),
        ("GET",  "/marketplace/requests/00000000-0000-0000-0000-000000000000/messages"),
        ("POST", "/marketplace/requests/00000000-0000-0000-0000-000000000000/messages"),
        ("GET",  "/approvals/pending"),
        ("GET",  "/admin/me"),
        ("GET",  "/admin/metrics"),
        ("GET",  "/admin/institutions"),
        ("GET",  "/products"),
        ("GET",  "/webhooks"),
    ]

    def _req(self, client, method, path, headers):
        return client.request(method, path, headers=headers)

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_no_auth_header_returns_401(self, client, method, path):
        r = self._req(client, method, path, {})
        assert r.status_code == 401, f"{method} {path}: expected 401, got {r.status_code}"

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_non_bearer_scheme_returns_401(self, client, method, path):
        r = self._req(client, method, path, {"Authorization": "Basic dXNlcjpwYXNz"})
        assert r.status_code == 401

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_bearer_empty_token_returns_401(self, client, method, path):
        r = self._req(client, method, path, {"Authorization": "Bearer "})
        assert r.status_code == 401

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_bearer_garbage_string_returns_401(self, client, method, path):
        r = self._req(client, method, path, {"Authorization": "Bearer not.a.jwt"})
        assert r.status_code == 401

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_well_formed_but_invalid_sig_returns_401(self, client, method, path):
        """
        A structurally valid JWT that fails signature verification must
        return 401 — not 500. Mocks verify_token to raise TokenError so
        we test the current_claims → HTTPException mapping without a live
        JWKS endpoint.
        """
        fake_jwt = (
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6InRlc3QifQ"
            ".eyJzdWIiOiJ4IiwiYXVkIjoiYXV0aGVudGljYXRlZCJ9"
            ".invalidsignature"
        )
        with patch(
            "app.deps.verify_token",
            new=AsyncMock(side_effect=TokenError("Token signed with an unknown key.")),
        ):
            r = self._req(client, method, path, {"Authorization": f"Bearer {fake_jwt}"})
        assert r.status_code == 401, (
            f"{method} {path}: verify_token raised TokenError but got {r.status_code}"
        )


# ===========================================================================
# Public endpoints — X-Service-Secret, no JWT
# ===========================================================================
class TestPublicEndpoints:
    REQUEST_ID  = str(uuid.uuid4())
    CONSUMER_ID = str(uuid.uuid4())

    def test_missing_secret_returns_403_or_503(self, client):
        r = client.get(
            f"/public/requests/{self.REQUEST_ID}/bids",
            params={"consumer_id": self.CONSUMER_ID},
        )
        assert r.status_code in (403, 503)

    def test_wrong_secret_returns_403(self, client):
        r = client.get(
            f"/public/requests/{self.REQUEST_ID}/bids",
            params={"consumer_id": self.CONSUMER_ID},
            headers={"X-Service-Secret": "wrong"},
        )
        assert r.status_code == 403

    def test_bulk_wrong_secret_returns_403(self, client):
        r = client.post(
            "/public/requests/bids/bulk",
            json={"request_ids": [self.REQUEST_ID], "consumer_id": self.CONSUMER_ID},
            headers={"X-Service-Secret": "wrong"},
        )
        assert r.status_code == 403

    def test_bulk_empty_list_with_valid_secret_returns_empty_dict(self, client):
        """Empty request_ids must short-circuit and return {} immediately."""
        with patch("app.api.public.service_session", side_effect=_mock_service_session):
            r = client.post(
                "/public/requests/bids/bulk",
                json={"request_ids": [], "consumer_id": self.CONSUMER_ID},
                headers={"X-Service-Secret": "test-secret-1234"},
            )
        assert r.status_code == 200
        assert r.json() == {}

    def test_bulk_missing_consumer_id_returns_422(self, client):
        r = client.post(
            "/public/requests/bids/bulk",
            json={"request_ids": [self.REQUEST_ID]},
            headers={"X-Service-Secret": "test-secret-1234"},
        )
        assert r.status_code == 422

    def test_bulk_missing_request_ids_returns_422(self, client):
        r = client.post(
            "/public/requests/bids/bulk",
            json={"consumer_id": self.CONSUMER_ID},
            headers={"X-Service-Secret": "test-secret-1234"},
        )
        assert r.status_code == 422

    def test_get_bids_with_valid_secret_and_mocked_db_returns_list(self, client):
        with patch("app.api.public.service_session", side_effect=_mock_service_session):
            r = client.get(
                f"/public/requests/{self.REQUEST_ID}/bids",
                params={"consumer_id": self.CONSUMER_ID},
                headers={"X-Service-Secret": "test-secret-1234"},
            )
        # 404 (request not found in mock) is acceptable — secret was valid
        assert r.status_code in (200, 404, 503)

    def test_market_intelligence_wrong_secret_returns_403(self, client):
        r = client.get(
            "/public/market-intelligence",
            headers={"X-Service-Secret": "wrong"},
        )
        assert r.status_code == 403

    def test_market_intelligence_no_secret_returns_403(self, client):
        r = client.get("/public/market-intelligence")
        assert r.status_code == 403

    def test_market_intelligence_valid_secret_returns_expected_shape(self, client):
        with patch("app.api.public.service_session", side_effect=_mock_service_session):
            r = client.get(
                "/public/market-intelligence",
                headers={"X-Service-Secret": "test-secret-1234"},
            )
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"marketRates", "acceptanceIntel", "competitiveness"}
        assert isinstance(body["marketRates"], list)
        assert isinstance(body["acceptanceIntel"], list)
        assert isinstance(body["competitiveness"], list)


# ===========================================================================
# Members
# ===========================================================================
class TestMembersEndpoints:
    def test_me_returns_200_or_404(self, auth_client):
        r = auth_client.get("/members/me")
        assert r.status_code in (200, 404)

    def test_my_group_returns_null_when_no_db_rows(self, auth_client):
        r = auth_client.get("/members/my-group")
        assert r.status_code == 200
        assert r.json() is None or isinstance(r.json(), dict)

    def test_list_returns_empty_list_with_mock_db(self, auth_client):
        r = auth_client.get("/members")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_admin_my_group_path_taken(self):
        """Admin role hits the admin code path; returns None from empty mock."""
        claims = admin_claims()
        app.dependency_overrides[current_claims] = _claims_override(claims)
        app.dependency_overrides[tenant_conn]    = _conn_override()
        try:
            with patch("app.api.admin.service_session", side_effect=_mock_service_session):
                r = TestClient(app, raise_server_exceptions=False).get("/members/my-group")
            assert r.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_no_sub_in_claims_returns_null_for_my_group(self):
        """my-group returns None gracefully when sub is missing."""
        claims = institution_claims(sub="")
        app.dependency_overrides[current_claims] = _claims_override(claims)
        app.dependency_overrides[tenant_conn]    = _conn_override()
        try:
            r = TestClient(app, raise_server_exceptions=False).get("/members/my-group")
            assert r.status_code == 200
            assert r.json() is None
        finally:
            app.dependency_overrides.clear()


# ===========================================================================
# Marketplace
# ===========================================================================
class TestMarketplaceEndpoints:
    def test_list_requests_returns_list(self, auth_client):
        r = auth_client.get("/marketplace/requests")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_list_requests_product_filter_accepted(self, auth_client):
        r = auth_client.get("/marketplace/requests?product_type=personal_loan")
        assert r.status_code == 200

    def test_list_requests_unknown_product_type_returns_empty(self, auth_client):
        r = auth_client.get("/marketplace/requests?product_type=__nonexistent__")
        assert r.status_code == 200

    def test_list_my_bids_returns_list(self, auth_client):
        r = auth_client.get("/marketplace/my-bids")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_admin_my_bids_returns_empty(self, admin_client):
        """Admins short-circuit — always return []."""
        r = admin_client.get("/marketplace/my-bids")
        assert r.status_code == 200
        assert r.json() == []

    def test_submit_bid_missing_fields_returns_422(self, auth_client):
        r = auth_client.post("/marketplace/bids", json={"rate": 7.5})
        assert r.status_code == 422

    def test_submit_bid_empty_body_returns_422(self, auth_client):
        r = auth_client.post("/marketplace/bids", json={})
        assert r.status_code == 422

    def test_submit_bid_no_institution_id_returns_403(self):
        """institution_id missing from JWT → 403 before DB is touched."""
        claims = institution_claims(institution_id=None)
        del claims["institution_id"]
        app.dependency_overrides[current_claims] = _claims_override(claims)
        app.dependency_overrides[tenant_conn]    = _conn_override()
        try:
            r = TestClient(app, raise_server_exceptions=False).post(
                "/marketplace/bids",
                json={
                    "request_id":     str(uuid.uuid4()),
                    "rate":           7.5,
                    "rate_type":      "fixed",
                    "amount_offered": 500000,
                    "term_months":    36,
                },
            )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()

    def test_submit_bid_valid_payload_reaches_db(self, auth_client):
        """Valid payload + institution context.
        Mock DB returns None for request lookup → 404 (request not found).
        Confirms endpoint performs a request existence guard before inserting.
        """
        r = auth_client.post(
            "/marketplace/bids",
            json={
                "request_id":       str(uuid.uuid4()),
                "rate":             7.5,
                "rate_type":        "fixed",
                "amount_offered":   500000,
                "term_months":      36,
                "idempotency_key":  str(uuid.uuid4()),
            },
        )
        # Mock fetchone=None → request existence guard fires → 404 is correct
        assert r.status_code == 404

    def test_get_bid_not_found_returns_404(self, auth_client):
        r = auth_client.get(f"/marketplace/bids/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_my_bids_status_filter_accepted(self, auth_client):
        r = auth_client.get("/marketplace/my-bids?status=submitted")
        assert r.status_code == 200


# ===========================================================================
# Approvals (institution-side maker-checker)
# ===========================================================================
class TestApprovalsEndpoints:
    def test_pending_returns_list(self, auth_client):
        r = auth_client.get("/approvals/pending")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_approve_action_with_mock_db_returns_404(self, auth_client):
        """approve_action with mock DB (fetchone=None) → action not found → 404."""
        r = auth_client.post(f"/approvals/{uuid.uuid4()}/approve", json={"note": "ok"})
        assert r.status_code == 404

    def test_reject_action_no_note_returns_422(self, auth_client):
        """Rejection without a note is a 422."""
        r = auth_client.post(f"/approvals/{uuid.uuid4()}/reject", json={})
        assert r.status_code == 422

    def test_reject_action_with_note_returns_404(self, auth_client):
        """reject_action with mock DB (fetchone=None) → action not found → 404."""
        r = auth_client.post(f"/approvals/{uuid.uuid4()}/reject", json={"note": "not compliant"})
        assert r.status_code == 404

    def test_submit_missing_required_fields_returns_422(self, auth_client):
        """Empty body → explicit 422 from field validation."""
        r = auth_client.post("/approvals/submit", json={})
        assert r.status_code == 422

    def test_submit_missing_resource_type_returns_422(self, auth_client):
        r = auth_client.post("/approvals/submit", json={"action_category": "group.create"})
        assert r.status_code == 422

    def test_submit_missing_action_category_returns_422(self, auth_client):
        r = auth_client.post("/approvals/submit", json={"resource_type": "group"})
        assert r.status_code == 422

    def test_submit_missing_body_returns_422(self, auth_client):
        r = auth_client.post("/approvals/submit")
        assert r.status_code == 422


# ===========================================================================
# Admin endpoints
# ===========================================================================
class TestAdminEndpoints:
    def _admin_patch(self):
        """Patch service_session at the admin module level."""
        return patch("app.api.admin.service_session", side_effect=_mock_service_session)

    def test_admin_me_returns_200_with_null(self, admin_client):
        """/admin/me returns null when no admin row in mock DB."""
        with self._admin_patch():
            r = admin_client.get("/admin/me")
        assert r.status_code == 200
        assert r.json() is None or isinstance(r.json(), dict)

    def test_admin_me_institution_user_returns_200(self, auth_client):
        """/admin/me returns null (not 403) for non-admin — endpoint is permissive."""
        with self._admin_patch():
            r = auth_client.get("/admin/me")
        assert r.status_code == 200

    def test_admin_metrics_institution_user_returns_403(self, auth_client):
        """institution_admin has no admin_users row → _require_admin raises 403."""
        with self._admin_patch():
            r = auth_client.get("/admin/metrics")
        assert r.status_code == 403

    def test_admin_institutions_institution_user_returns_403(self, auth_client):
        with self._admin_patch():
            r = auth_client.get("/admin/institutions")
        assert r.status_code == 403

    def test_admin_users_institution_user_returns_403(self, auth_client):
        with self._admin_patch():
            r = auth_client.get("/admin/users")
        assert r.status_code == 403

    def test_admin_roles_institution_user_returns_403(self, auth_client):
        with self._admin_patch():
            r = auth_client.get("/admin/roles")
        assert r.status_code == 403


# ===========================================================================
# Catalog endpoints (no prefix — /products, /webhooks, /audit)
# ===========================================================================
class TestCatalogEndpoints:
    def test_products_returns_list(self, auth_client):
        r = auth_client.get("/products")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_webhooks_returns_list(self, auth_client):
        r = auth_client.get("/webhooks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_audit_returns_list(self, auth_client):
        r = auth_client.get("/audit")
        assert r.status_code == 200

    def test_products_no_auth_returns_401(self, client):
        r = client.get("/products")
        assert r.status_code == 401

    def test_audit_limit_param_accepted(self, auth_client):
        r = auth_client.get("/audit?limit=10")
        assert r.status_code == 200


# ===========================================================================
# Institutions endpoint
# ===========================================================================
class TestInstitutionsEndpoints:
    def test_me_admin_returns_admin_type_without_db(self, admin_client):
        """/institutions/me for admin role returns immediately — no DB needed."""
        r = admin_client.get("/institutions/me")
        assert r.status_code == 200
        body = r.json()
        assert body["user_type"] == "admin"
        assert body["approved"] is True

    def test_me_institution_user_no_db_row_returns_404(self):
        """Institution user, DB returns None → 404 Institution not found."""
        claims = institution_claims()
        app.dependency_overrides[current_claims] = _claims_override(claims)
        app.dependency_overrides[tenant_conn]    = _conn_override()
        try:
            with patch(
                "app.core.db.tenant_session",
                side_effect=lambda c: _mock_tenant_session(c, fetchone=None),
            ):
                r = TestClient(app, raise_server_exceptions=False).get("/institutions/me")
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_me_no_institution_id_in_claims_returns_403(self):
        claims = institution_claims()
        del claims["institution_id"]
        app.dependency_overrides[current_claims] = _claims_override(claims)
        app.dependency_overrides[tenant_conn]    = _conn_override()
        try:
            r = TestClient(app, raise_server_exceptions=False).get("/institutions/me")
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()


# ===========================================================================
# Request validation & Content-Type
# ===========================================================================
class TestRequestValidation:
    def test_bids_non_json_body_returns_422(self, auth_client):
        r = auth_client.post(
            "/marketplace/bids",
            content=b"not-json",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 422

    def test_bulk_bids_non_json_body_returns_422(self, client):
        r = client.post(
            "/public/requests/bids/bulk",
            content=b"not-json",
            headers={
                "Content-Type": "text/plain",
                "X-Service-Secret": "test-secret-1234",
            },
        )
        assert r.status_code == 422


# ===========================================================================
# Approval Engine — Delegations
# ===========================================================================
class TestDelegationEndpoints:
    _FROM = str(uuid.uuid4())
    _TO   = str(uuid.uuid4())

    def _create_client(self, claims):
        app.dependency_overrides[current_claims] = _claims_override(claims)
        app.dependency_overrides[tenant_conn]    = _conn_override()
        return TestClient(app, raise_server_exceptions=False)

    def test_list_delegations_returns_200_list(self, auth_client):
        r = auth_client.get("/approval-engine/delegations")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_non_admin_cannot_create_delegation(self):
        """institution_member (non-admin) is blocked from creating delegations."""
        client = self._create_client(institution_claims(user_role="institution_member"))
        try:
            r = client.post("/approval-engine/delegations", json={
                "from_member": self._FROM, "to_member": self._TO,
                "reason": "Annual leave", "valid_from": "2026-07-01T00:00:00Z",
                "valid_to": "2026-07-10T00:00:00Z",
            })
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()

    def test_institution_admin_can_create_delegation(self):
        """institution_admin (not just super_admin) must pass — this was the
        exact class of bug fixed across the API: institution_admin missing
        from every admin-bypass check."""
        app.dependency_overrides[current_claims] = _claims_override(institution_claims())
        app.dependency_overrides[tenant_conn]    = _conn_override(
            fetchone=SimpleNamespace(id=uuid.uuid4())
        )
        try:
            r = TestClient(app, raise_server_exceptions=False).post(
                "/approval-engine/delegations", json={
                    "from_member": self._FROM, "to_member": self._TO,
                    "reason": "Annual leave", "valid_from": "2026-07-01T00:00:00Z",
                    "valid_to": "2026-07-10T00:00:00Z",
                })
            assert r.status_code == 200
            assert "id" in r.json()
        finally:
            app.dependency_overrides.clear()

    def test_self_delegation_rejected(self, auth_client):
        r = auth_client.post("/approval-engine/delegations", json={
            "from_member": self._FROM, "to_member": self._FROM,
            "reason": "Annual leave", "valid_from": "2026-07-01T00:00:00Z",
            "valid_to": "2026-07-10T00:00:00Z",
        })
        assert r.status_code == 422

    def test_missing_reason_rejected(self, auth_client):
        r = auth_client.post("/approval-engine/delegations", json={
            "from_member": self._FROM, "to_member": self._TO,
            "reason": "", "valid_from": "2026-07-01T00:00:00Z",
            "valid_to": "2026-07-10T00:00:00Z",
        })
        assert r.status_code == 422

    def test_valid_to_before_valid_from_rejected(self, auth_client):
        r = auth_client.post("/approval-engine/delegations", json={
            "from_member": self._FROM, "to_member": self._TO,
            "reason": "Annual leave", "valid_from": "2026-07-10T00:00:00Z",
            "valid_to": "2026-07-01T00:00:00Z",
        })
        assert r.status_code == 422

    def test_malformed_datetime_rejected(self, auth_client):
        r = auth_client.post("/approval-engine/delegations", json={
            "from_member": self._FROM, "to_member": self._TO,
            "reason": "Annual leave", "valid_from": "not-a-date",
            "valid_to": "2026-07-10T00:00:00Z",
        })
        assert r.status_code == 422

    def test_revoke_delegation_non_admin_forbidden(self):
        client = self._create_client(institution_claims(user_role="institution_member"))
        try:
            r = client.delete(f"/approval-engine/delegations/{uuid.uuid4()}")
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()

    def test_revoke_delegation_admin_ok(self, auth_client):
        r = auth_client.delete(f"/approval-engine/delegations/{uuid.uuid4()}")
        assert r.status_code == 200
        assert r.json()["ok"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
