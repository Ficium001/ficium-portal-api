"""Signed download links for compliance documents. The bucket is private; the
screens used to link to a public URL that never worked."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.deps import current_claims, tenant_conn
from app.main import app

DOC = str(uuid.uuid4())
ROW = SimpleNamespace(storage_path="inst/x/doc.pdf", file_name="licence.pdf")


class Conn:
    def __init__(self, doc: Any = ROW, admin: bool = True) -> None:
        self.doc, self.admin = doc, admin

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        q = " ".join(str(stmt).split())
        if "portal_admin.admin_users" in q:
            admin = SimpleNamespace(id="a1") if self.admin else None
            return SimpleNamespace(fetchone=lambda: admin)
        return SimpleNamespace(fetchone=lambda: self.doc)


async def _signed(path: str, expires_in: int = 600, **_: Any) -> str:
    return f"https://signed.example/{path}?exp={expires_in}"


def _client(conn: Conn, perms: list[str]) -> TestClient:
    async def claims() -> dict[str, Any]:
        return {"sub": str(uuid.uuid4()), "institution_id": str(uuid.uuid4()),
                "module_permissions": perms}

    def tenant(claims: Any = None):  # noqa: ANN202
        yield conn

    app.dependency_overrides[current_claims] = claims
    app.dependency_overrides[tenant_conn] = tenant
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    yield
    app.dependency_overrides.clear()


def test_bank_gets_a_short_lived_link_to_its_own_document() -> None:
    with patch("app.api.documents.storage_signed_url", _signed):
        r = _client(Conn(), ["inst:documents"]).get(f"/documents/{DOC}/download")
    assert r.status_code == 200, r.text
    assert r.json()["url"].endswith("exp=300") and r.json()["file_name"] == "licence.pdf"


def test_document_hidden_by_rls_is_a_404() -> None:
    with patch("app.api.documents.storage_signed_url", _signed):
        r = _client(Conn(doc=None), ["inst:documents"]).get(f"/documents/{DOC}/download")
    assert r.status_code == 404


def test_bank_needs_the_documents_module() -> None:
    assert _client(Conn(), ["inst:bids"]).get(f"/documents/{DOC}/download").status_code == 403


def _admin_get(conn: Conn) -> Any:
    @contextmanager
    def svc():  # noqa: ANN202
        yield conn

    with patch("app.api.admin.service_session", svc), \
         patch("app.core.storage.storage_signed_url", _signed):
        return _client(conn, []).get(f"/admin/documents/{DOC}/download")


def test_admin_review_link() -> None:
    r = _admin_get(Conn())
    assert r.status_code == 200 and r.json()["url"].endswith("exp=300")


def test_non_admin_cannot_use_the_review_link() -> None:
    assert _admin_get(Conn(admin=False)).status_code == 403


@pytest.fixture(autouse=True)
def _legacy_admin_on(monkeypatch):
    """These tests cover the retired /admin/* routes, which are off by default."""
    from app.core.config import settings as _s

    monkeypatch.setattr(_s, "legacy_admin_enabled", True)
