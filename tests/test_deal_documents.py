"""
Document checklist (db/027) — what Python owns: permission and ownership
checks, file validation, error mapping, and the stage gate.

The transitions themselves (four eyes on verify, notes on reject/waive,
closed deals frozen, verified files not replaceable, gate count) are SQL and
were exercised against the live schema in a rolled-back transaction before
the migration was applied.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.deps import current_claims  # noqa: E402
from app.main import app  # noqa: E402

INST = "31b3ee32-2864-4875-8920-cc5f27240971"
USER = "4224540d-d59c-4584-8271-cb6ef24c472d"
DEAL = "5a0c7c1e-1111-4222-8333-944455556666"
DOC = "9b0c7c1e-2222-4222-8333-944455556666"
TPL = "7b6e2c4c-0000-4000-8000-000000000001"
PREFIX = f"{INST}/deals/{DEAL}/{DOC}/"


class _R:
    def __init__(self, row: dict | None = None, scalar: Any = None,
                 rows: list | None = None) -> None:
        self.row, self._scalar, self.rows = row, scalar, rows or []

    def first(self) -> Any:
        if self.row is None:
            return None
        return type("Row", (), {**self.row, "_mapping": self.row})

    def fetchone(self) -> Any:
        return self.first()

    def mappings(self) -> _M:
        return _M(self)

    def all(self) -> list:
        return self.rows

    def one(self) -> dict:
        return self.row or {}

    def scalar_one(self) -> Any:
        return self._scalar


class _M:
    """Row-as-dict view, like SQLAlchemy's .mappings()."""

    def __init__(self, r: _R) -> None:
        self.r = r

    def first(self) -> dict | None:
        return dict(self.r.row) if self.r.row is not None else None

    def one(self) -> dict:
        return dict(self.r.row or {})

    def all(self) -> list:
        return self.r.rows


class Fake:
    def __init__(self, *, doc_status: str = "received", modules: list[str] | None = None,
                 act_error: str | None = None, outstanding: int = 0,
                 stage_ok: bool = True) -> None:
        self.doc_status, self.modules, self.act_error = doc_status, modules or [], act_error
        self.outstanding, self.stage_ok = outstanding, stage_ok
        self.sql: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> _R:
        q = str(stmt)
        self.sql.append(q)
        if "FROM institution.institution" in q:
            return _R({"modules": ["pipeline"]})
        if ("FROM marketplace.deal_document" in q and "deal_document_act" not in q
                and "SELECT id, status" in q):
            return _R({"id": DOC, "status": self.doc_status, "storage_path": PREFIX + "a.pdf",
                       "file_name": "a.pdf"})
        if "deal_document_act" in q:
            if self.act_error:
                raise Exception(f"ERROR: {self.act_error}")
            return _R(scalar={"id": DOC, "status": "verified"})
        if "module_permissions" in q:
            return _R({"modules": self.modules})
        if "FROM institution.pipeline_template" in q:
            return _R({"x": 1})
        if "FROM institution.pipeline_stage_def" in q:
            return _R({"x": 1} if self.stage_ok else None)
        if "stage_documents_outstanding" in q:
            return _R(scalar=self.outstanding)
        if "SELECT lp.id, lp.status" in q:
            return _R({"id": DEAL, "status": "active", "current_stage_id": "s1"})
        if "FROM marketplace.pipeline_stage_instance psi" in q:
            return _R({"id": "s1", "status": "active", "requires_maker_checker": False,
                       "approval_route": False, "label": "Credit docs", "position": 1})
        if "INSERT INTO institution.doc_requirement" in q:
            return _R({"id": "r1", "name": params["name"]})
        return _R({"x": 1})

    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@contextmanager
def client(fake: Fake):
    async def claims() -> dict[str, Any]:
        return {"sub": USER, "institution_id": INST, "user_role": "maker"}

    @contextmanager
    def svc():
        yield fake

    app.dependency_overrides[current_claims] = claims
    try:
        with patch("app.deps.service_session", svc), \
             patch("app.api.deal_documents.service_session", svc), \
             patch("app.api.pipeline.service_session", svc), \
             patch("app.api.deal_documents.storage_signed_upload_url",
                   AsyncMock(return_value={"upload_url": "u", "storage_path": PREFIX + "x.pdf"})):
            yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def test_upload_url_rejects_unsupported_type_and_size() -> None:
    with client(Fake(doc_status="required")) as c:
        bad_type = c.post(f"/pipelines/{DEAL}/documents/{DOC}/upload-url",
                          json={"file_name": "x.exe", "file_size": 10,
                                "mime_type": "application/x-msdownload"})
        too_big = c.post(f"/pipelines/{DEAL}/documents/{DOC}/upload-url",
                         json={"file_name": "x.pdf", "mime_type": "application/pdf",
                               "file_size": 30 * 1024 * 1024})
        ok = c.post(f"/pipelines/{DEAL}/documents/{DOC}/upload-url",
                    json={"file_name": "x.pdf", "mime_type": "application/pdf", "file_size": 1000})
    assert bad_type.status_code == 422 and too_big.status_code == 422
    assert ok.status_code == 200 and ok.json()["storage_path"].startswith(PREFIX)


def test_upload_url_refused_for_verified_document() -> None:
    with client(Fake(doc_status="verified")) as c:
        r = c.post(f"/pipelines/{DEAL}/documents/{DOC}/upload-url",
                   json={"file_name": "x.pdf", "mime_type": "application/pdf", "file_size": 1000})
    assert r.status_code == 409


def test_receive_refuses_a_file_from_elsewhere() -> None:
    f = Fake()
    with client(f) as c:
        r = c.post(f"/pipelines/{DEAL}/documents/{DOC}/receive",
                   json={"storage_path": f"{INST}/deals/other/{DOC}/x.pdf", "file_name": "x.pdf"})
    assert r.status_code == 422
    assert not any("deal_document_act" in q for q in f.sql)


def test_receive_accepts_its_own_file_and_audits() -> None:
    f = Fake()
    with client(f) as c:
        r = c.post(f"/pipelines/{DEAL}/documents/{DOC}/receive",
                   json={"storage_path": PREFIX + "x.pdf", "file_name": "x.pdf",
                         "mime_type": "application/pdf", "file_size": 1000})
    assert r.status_code == 200, r.text
    assert any("audit.event" in q for q in f.sql)


def test_rule_errors_are_mapped() -> None:
    cases = {"DOC_FOUR_EYES": 403, "DOC_NOTE_REQUIRED": 422, "DOC_DEAL_CLOSED": 409,
             "DOC_NOT_RECEIVED": 409, "DOC_ALREADY_VERIFIED": 409}
    for code, status in cases.items():
        with client(Fake(act_error=code)) as c:
            r = c.post(f"/pipelines/{DEAL}/documents/{DOC}/verify", json={})
        assert r.status_code == status, code


def test_stage_cannot_advance_with_outstanding_documents() -> None:
    with client(Fake(outstanding=2)) as c:
        r = c.post(f"/pipelines/{DEAL}/stages/s1/advance", json={})
    assert r.status_code == 409
    assert "2 required documents" in r.json()["detail"]


def test_stage_advances_when_documents_are_done() -> None:
    with client(Fake(outstanding=0)) as c, \
         patch("app.api.pipeline._complete_stage", return_value={"status": "advanced"}) as done:
        r = c.post(f"/pipelines/{DEAL}/stages/s1/advance", json={})
    assert r.status_code == 200 and done.called


def test_configuring_the_checklist_needs_settings_permission() -> None:
    with client(Fake(modules=["inst:pipeline"])) as c:
        r = c.post(f"/pipelines/templates/{TPL}/documents", json={"name": "Payslips"})
    assert r.status_code == 403


def test_requirement_stage_must_belong_to_the_workflow() -> None:
    with client(Fake(modules=["inst:settings"], stage_ok=False)) as c:
        r = c.post(f"/pipelines/templates/{TPL}/documents",
                   json={"name": "Payslips",
                         "stage_def_id": "5a0c7c1e-0000-4000-8000-000000000009"})
    assert r.status_code == 422


def test_requirement_created() -> None:
    with client(Fake(modules=["inst:settings"])) as c:
        r = c.post(f"/pipelines/templates/{TPL}/documents",
                   json={"name": "Payslips", "provided_by": "borrower", "mandatory": True})
    assert r.status_code == 201, r.text
