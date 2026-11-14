"""A checker can open a template version before approving it, and gets exactly the file that
was uploaded (checksum recorded at upload), or an error."""
from __future__ import annotations

import hashlib
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app import deps  # noqa: E402
from app.api.doc_templates import router as dt  # noqa: E402
from app.deps import current_claims, tenant_conn  # noqa: E402
from app.main import app  # noqa: E402

DOCX = b"PK\x03\x04 fake docx bytes"
IID = str(uuid.uuid4())


class Conn:
    def __init__(self, row: dict | None) -> None:
        self.row, self.audits = row, 0

    def execute(self, stmt: Any, params: dict | None = None) -> Any:
        q = str(stmt)
        if "doc_template_version" in q and q.lstrip().upper().startswith("SELECT"):
            row = SimpleNamespace(_mapping=self.row) if self.row else None
            return SimpleNamespace(fetchone=lambda: row)
        self.audits += 1
        return SimpleNamespace(fetchone=lambda: None)

    def commit(self) -> None: ...


@pytest.fixture()
def call(monkeypatch: pytest.MonkeyPatch):
    async def stored(path: str) -> bytes:
        return DOCX

    monkeypatch.setattr(dt, "_storage_download", stored)
    monkeypatch.setattr(deps, "_institution_modules", lambda iid: [dt.MODULE_KEY])

    def run(row: dict | None):
        conn = Conn(row)

        async def claims() -> dict:
            return {"sub": str(uuid.uuid4()), "institution_id": IID, "user_role": "checker"}

        def tenant():  # noqa: ANN202
            yield conn

        app.dependency_overrides[current_claims] = claims
        app.dependency_overrides[tenant_conn] = tenant
        try:
            url = f"/institution/doc-templates/{uuid.uuid4()}/versions/{uuid.uuid4()}/download"
            return TestClient(app, raise_server_exceptions=False).get(url), conn
        finally:
            app.dependency_overrides.clear()
    return run


def test_checker_gets_the_uploaded_file(call: Any) -> None:
    good = hashlib.sha256(DOCX).hexdigest()
    r, conn = call({"file_name": "loan.docx", "storage_path": "p", "checksum_sha256": good})
    assert r.status_code == 200, r.text
    assert r.content == DOCX
    assert 'filename="loan.docx"' in r.headers["content-disposition"]
    assert conn.audits == 1


def test_a_file_that_no_longer_matches_is_refused(call: Any) -> None:
    r, _ = call({"file_name": "loan.docx", "storage_path": "p", "checksum_sha256": "0" * 64})
    assert r.status_code == 409
    assert "Do not approve" in r.json()["detail"]


def test_unknown_version_is_404(call: Any) -> None:
    r, _ = call(None)
    assert r.status_code == 404
