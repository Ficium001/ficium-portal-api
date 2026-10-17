"""Contract 1.5: borrower document uploads (institution side)."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, patch

import ficium_contract as fc
import pytest
from fastapi.testclient import TestClient

from app.api import deal_document_uploads as m
from app.main import app

KEY = "k-borrower"
P = "00000000-0000-4000-8000-000000000030"
A = "00000000-0000-4000-8000-000000000099"
R = "00000000-0000-4000-8000-000000000040"
U = "00000000-0000-4000-8000-000000000050"
INST = "00000000-0000-4000-8000-000000000060"
D = "00000000-0000-4000-8000-000000000070"
PDF = b"%PDF-1.7 three payslips"
SHA = hashlib.sha256(PDF).hexdigest()
ITEM = {
    "requirement_id": R,
    "name": "Proof of income",
    "description": None,
    "provided_by": "borrower",
    "mandatory": True,
    "stage_label": "Credit Documentation",
    "status": "received",
    "review_note": None,
    "document_type": "payslip",
    "max_age_days": 90,
}
SLOT = {
    "pipeline_id": P,
    "anon_borrower_id": A,
    "requirement_id": R,
    "file_name": "payslips.pdf",
    "mime_type": "application/pdf",
    "size_bytes": len(PDF),
    "source": "new",
}


class _R:
    def __init__(self, row: Any = None, scalar: Any = None) -> None:
        self.row, self.scalar = row, scalar

    def fetchone(self) -> Any:
        return self.row

    def one(self) -> Any:
        assert self.row is not None
        return self.row

    def scalar_one_or_none(self) -> Any:
        return self.scalar


class Conn:
    def __init__(self, st: dict[str, Any]) -> None:
        self.st, self.sql = st, []

    def execute(self, stmt: Any, params: Any = None) -> _R:
        q = str(stmt)
        self.sql.append((q, params))
        st = self.st
        if "integration.is_phase2" in q:
            return _R(st.get("deal"))
        if "FROM marketplace.deal_document dd" in q:
            return _R(st.get("doc"))
        if "INSERT INTO integration.document_upload" in q:
            return _R(NS(upload_id=U, expires_at=datetime(2026, 10, 17, 10, 15, tzinfo=UTC)))
        if (
            "FROM integration.document_upload" in q
            and "FOR UPDATE" not in q
            and "UPDATE integration" not in q
            and "INSERT" not in q
        ):
            return _R(st.get("upload"))
        if "FOR UPDATE" in q:
            return _R(NS(status=st["upload"].status, expired=st.get("expired", False)))
        if "build_deal_documents_payload" in q:
            return _R(scalar={"documents": [ITEM]})
        if "deal_document_act" in q and st.get("act_error"):
            raise RuntimeError(st["act_error"])
        return _R()

    def commit(self) -> None:
        self.st["committed"] = True

    def rollback(self) -> None:
        self.st["rolled_back"] = True


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(m.settings, "integration_documents_enabled", True)
    monkeypatch.setattr(m.settings, "integration_public_base_url", "https://api.portal.example")
    monkeypatch.setattr(m.settings, "integration_acceptance_verify_keys", KEY)
    st: dict[str, Any] = {
        "deal": NS(id=P, institution_id=INST, status="active"),
        "doc": NS(id=D, status="required", provided_by="borrower", max_age_days=90),
        "upload": NS(
            upload_id=U,
            status="open",
            pipeline_id=P,
            deal_document_id=D,
            requirement_id=R,
            institution_id=INST,
            storage_path=f"{INST}/deals/{P}/{D}/borrower-x.pdf",
            file_name="payslips.pdf",
            mime_type="application/pdf",
            size_bytes=len(PDF),
            source="vault",
            document_date=date(2026, 9, 30),
            content_sha256=SHA,
            expired=False,
        ),
        "conns": [],
    }

    @contextmanager
    def svc():
        c = Conn(st)
        st["conns"].append(c)
        yield c

    with (
        patch.object(m, "service_session", svc),
        patch.object(m, "storage_upload", AsyncMock()) as stored,
        patch.object(m.settings, "integration_documents_enabled", True),
    ):
        st["stored"] = stored
        yield TestClient(app, raise_server_exceptions=False), st


def post(client: TestClient, path: str, body: dict, key: str = KEY):
    raw = json.dumps(body).encode()
    return client.post(
        path,
        content=raw,
        headers={
            fc.SIGNATURE_HEADER: fc.sign(raw, key.encode()),
            "Content-Type": "application/json",
        },
    )


def sqls(st: dict) -> list[str]:
    return [q for c in st["conns"] for q, _ in c.sql]


URL = "/integration/v1/deal-documents/uploads"


def test_off_unless_enabled(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(m.settings, "integration_documents_enabled", False)
    assert post(client, URL, SLOT).status_code == 503


def test_signature_and_contract(env):
    client, _ = env
    assert post(client, URL, SLOT, key="wrong").status_code == 401
    assert post(client, URL, {**SLOT, "mime_type": "application/x-msdownload"}).status_code == 422
    assert (
        post(client, URL, {**SLOT, "source": "vault"}).status_code == 422
    )  # vault copy without its date


def test_only_the_owner_of_a_phase2_deal(env):
    client, st = env
    st["deal"] = None
    r = post(client, URL, SLOT)
    assert r.status_code == 404 and r.json()["error"] == "deal_not_found"


@pytest.mark.parametrize(
    ("doc", "deal_status", "code"),
    [
        ({"provided_by": "bank"}, "active", "not_provided_by_borrower"),
        ({"status": "verified"}, "active", "document_verified"),
        ({}, "completed", "deal_not_active"),
    ],
)
def test_refusals(env, doc, deal_status, code):
    client, st = env
    st["doc"] = NS(**{**vars(st["doc"]), **doc})
    st["deal"] = NS(**{**vars(st["deal"]), "status": deal_status})
    r = post(client, URL, SLOT)
    assert r.status_code in (409,) and r.json()["error"] == code


def test_vault_copy_older_than_max_age_is_refused_here_too(env):
    client, _ = env
    old = (date.today() - timedelta(days=91)).isoformat()
    r = post(client, URL, {**SLOT, "source": "vault", "document_date": old})
    assert r.status_code == 422 and r.json()["error"] == "document_too_old"
    ok = (date.today() - timedelta(days=90)).isoformat()
    assert post(client, URL, {**SLOT, "source": "vault", "document_date": ok}).status_code == 201


def test_slot_points_at_the_interface_never_storage(env):
    client, _ = env
    r = post(client, URL, SLOT)
    assert r.status_code == 201, r.text
    body = r.json()
    fc.validate_document_upload_response(body)  # the contract pattern forbids storage URLs
    assert body["upload_url"] == f"https://api.portal.example{URL}/{U}/content"
    assert "supabase" not in body["upload_url"]


def test_slot_needs_an_https_public_origin(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(m.settings, "integration_public_base_url", "")
    assert post(client, URL, SLOT).status_code == 503


# ── content: bytes arrive on the interface, signed ─────────────────────────────
def put(
    client: TestClient, data: bytes, key: str = KEY, ctype: str = "application/pdf", uid: str = U
):
    return client.put(
        f"{URL}/{uid}/content",
        content=data,
        headers={fc.SIGNATURE_HEADER: fc.sign(data, key.encode()), "Content-Type": ctype},
    )


def test_content_is_signed_checked_and_stored_by_this_api(env):
    client, st = env
    st["upload"] = NS(**{**vars(st["upload"]), "content_sha256": None})
    assert put(client, PDF, key="wrong").status_code == 401
    r = put(client, PDF)
    assert r.status_code == 200, r.text
    st["stored"].assert_awaited_once_with(st["upload"].storage_path, PDF, "application/pdf")
    assert any("content_sha256 = :sha" in q for q in sqls(st))


@pytest.mark.parametrize(
    ("data", "ctype", "status", "err"),
    [
        (PDF + b"x", "application/pdf", 422, "size_mismatch"),
        (b"MZ" + b"\x00" * (len(PDF) - 2), "application/pdf", 422, "type_mismatch"),
        (PDF, "image/png", 415, "content_type_mismatch"),
    ],
)
def test_content_refusals(env, data, ctype, status, err):
    client, st = env
    r = put(client, data, ctype=ctype)
    assert r.status_code == status and r.json()["error"] == err
    st["stored"].assert_not_awaited()


def test_content_too_large_is_refused_before_reading(env):
    client, st = env
    r = client.put(
        f"{URL}/{U}/content",
        content=b"x",
        headers={"Content-Length": str(m.MAX_UPLOAD_BYTES + 1), "Content-Type": "application/pdf"},
    )
    assert r.status_code in (413, 400)
    st["stored"].assert_not_awaited()


def test_content_after_expiry_or_completion(env):
    client, st = env
    st["upload"] = NS(**{**vars(st["upload"]), "expired": True})
    assert put(client, PDF).status_code == 410
    st["upload"] = NS(**{**vars(st["upload"]), "expired": False, "status": "completed"})
    assert put(client, PDF).status_code == 409


# ── complete ──────────────────────────────────────────────────────────────────
CPL = {"upload_id": U, "pipeline_id": P, "anon_borrower_id": A, "sha256": SHA}


def test_complete_needs_the_content_first(env):
    client, st = env
    st["upload"] = NS(**{**vars(st["upload"]), "content_sha256": None})
    r = post(client, f"{URL}/{U}/complete", CPL)
    assert r.status_code == 409 and r.json()["error"] == "content_not_received"


def test_complete_hash_must_match_what_arrived(env):
    client, st = env
    r = post(client, f"{URL}/{U}/complete", {**CPL, "sha256": "b" * 64})
    assert r.status_code == 422 and r.json()["error"] == "hash_mismatch"
    assert not any("deal_document_act" in q for q in sqls(st))


def test_complete_registers_as_borrower_upload(env):
    client, st = env
    r = post(client, f"{URL}/{U}/complete", CPL)
    assert r.status_code == 200, r.text
    fc.validate_document_complete_response(r.json())
    qs = sqls(st)
    act = next(q for q in qs if "deal_document_act" in q)
    assert "'receive', NULL" in act  # no staff actor: four-eyes leaves any officer free to verify
    assert any("uploaded_via = 'borrower'" in q for q in qs)
    assert any("'borrower', CAST(:i AS uuid)" in q for q in qs)
    assert st["committed"]


def test_complete_is_one_time_and_expires(env):
    client, st = env
    st["upload"] = NS(**{**vars(st["upload"]), "status": "completed"})
    assert post(client, f"{URL}/{U}/complete", CPL).status_code == 409
    st["upload"] = NS(**{**vars(st["upload"]), "status": "open"})
    st["expired"] = True
    assert post(client, f"{URL}/{U}/complete", CPL).status_code == 410


def test_closed_deal_at_completion_is_a_409(env):
    client, st = env
    st["act_error"] = "ERROR: DOC_DEAL_CLOSED"
    r = post(client, f"{URL}/{U}/complete", CPL)
    assert r.status_code == 409 and r.json()["error"] == "doc_deal_closed"
    assert st.get("rolled_back")


def test_path_and_body_upload_ids_must_match(env):
    client, _ = env
    other = "00000000-0000-4000-8000-000000000051"
    assert post(client, f"{URL}/{other}/complete", CPL).status_code == 422


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ({"document_type": "payslip", "max_age_days": 90}, True),
        ({"document_type": None, "max_age_days": ""}, True),
        ({"document_type": "pct_payslip"}, False),
        ({"max_age_days": 0}, False),
        ({"max_age_days": "ninety"}, False),
    ],
)
def test_requirement_type_and_max_age_validation(body, ok):
    from fastapi import HTTPException

    from app.api.deal_documents import _clean

    try:
        out = _clean(body, None, "t", partial=True)  # type: ignore[arg-type]
    except HTTPException:
        assert not ok
    else:
        assert ok
        assert set(out) == set(body)
