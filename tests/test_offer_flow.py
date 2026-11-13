"""
Offers and agreements simplification: server-side approval gate, one-step
send-offer readiness, setup checklist, and upload-time merge-tag validation.
No database required: SQL is answered by a fake connection keyed on text.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from docx import Document  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.api import esign, offer_flow, offer_setup  # noqa: E402
from app.api.doc_templates import merge_engine, starter  # noqa: E402
from app.api.doc_templates import router as dt_router  # noqa: E402
from app.core import offer_gate  # noqa: E402

IID = "11111111-1111-1111-1111-111111111111"
PID = "22222222-2222-2222-2222-222222222222"


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows


class FakeConn:
    """Answers each SQL statement with the rows of the first matching key."""

    def __init__(self, answers: dict[str, list[dict]]) -> None:
        self.answers = answers

    def execute(self, sql: Any, params: dict | None = None) -> _Result:
        q = str(sql)
        for key, rows in self.answers.items():
            if key in q:
                return _Result([SimpleNamespace(**r) for r in rows])
        return _Result([])

    def commit(self) -> None: ...
    def rollback(self) -> None: ...


DEAL = {"FROM marketplace.loan_pipeline\n        WHERE id": [{"x": 1}]}


# ── Approval gate messages ───────────────────────────────────────────────────

def test_no_approval_says_what_to_do() -> None:
    s = offer_gate.deal_approval(FakeConn({}), IID, PID)
    assert s.state == "none" and not s.approved
    assert "no credit approval yet" in s.message


def test_approved_returns_instance() -> None:
    conn = FakeConn({"FROM institution.approval_instance": [
        {"id": "inst-1", "status": "approved", "resolved_at": None}]})
    s = offer_gate.deal_approval(conn, IID, PID)
    assert s.approved and s.instance_id == "inst-1"


def test_pending_names_step_and_people() -> None:
    conn = FakeConn({
        "FROM institution.approval_instance": [
            {"id": "inst-1", "status": "in_progress", "resolved_at": None}],
        "FROM institution.approval_stage_instance": [
            {"seq": 2, "due_at": datetime(2026, 11, 14, 9, 0, tzinfo=UTC),
             "name": "Credit review", "approver_role": "checker", "committee_name": None}],
        "FROM institution.member": [{"full_name": "Asha R"}, {"full_name": "Dev P"}],
    })
    s = offer_gate.deal_approval(conn, IID, PID)
    assert s.state == "pending"
    assert 'step 2 "Credit review"' in s.message
    assert "Asha R or Dev P ('checker' role)" in s.message
    assert "14 Nov 2026 09:00 UTC" in s.message


def test_pending_committee_named() -> None:
    conn = FakeConn({
        "FROM institution.approval_instance": [
            {"id": "i", "status": "in_progress", "resolved_at": None}],
        "FROM institution.approval_stage_instance": [
            {"seq": 1, "due_at": None, "name": "Committee", "approver_role": None,
             "committee_name": "Retail Credit"}],
    })
    assert "the Retail Credit committee" in offer_gate.deal_approval(conn, IID, PID).message


def test_pending_role_with_nobody_says_so() -> None:
    conn = FakeConn({
        "FROM institution.approval_instance": [
            {"id": "i", "status": "in_progress", "resolved_at": None}],
        "FROM institution.approval_stage_instance": [
            {"seq": 1, "due_at": None, "name": "Credit", "approver_role": "checker",
             "committee_name": None}],
    })
    assert "nobody on your team has that role" in offer_gate.deal_approval(conn, IID, PID).message


def test_rejected_says_resubmit() -> None:
    conn = FakeConn({"FROM institution.approval_instance": [
        {"id": "i", "status": "rejected", "resolved_at": None}]})
    s = offer_gate.deal_approval(conn, IID, PID)
    assert s.state == "rejected" and "Resubmit" in s.message


def test_loan_agreement_is_an_offer_not_custom() -> None:
    assert offer_gate.ENVELOPE_TYPE_FOR_CATEGORY["loan_agreement"] == "offer_letter"
    assert offer_gate.ENVELOPE_TYPE_FOR_CATEGORY["facility_letter"] == "offer_letter"


# ── E-sign: the client's label can no longer bypass the gate ────────────────

@pytest.mark.parametrize("label", ["custom", "offer_letter"])
async def test_deal_envelope_gated_whatever_the_label(monkeypatch, label) -> None:
    async def _no_download(_path: str) -> bytes:
        raise AssertionError("must refuse before touching the document")

    monkeypatch.setattr(esign, "_storage_download", _no_download)
    claims = {"institution_id": IID, "sub": "u", "user_role": "super_admin"}
    with pytest.raises(HTTPException) as exc:
        await esign.create_envelope(
            body={"entity_type": label, "entity_id": PID, "document_path": "x.pdf"},
            conn=FakeConn(DEAL), claims=claims,
        )
    assert exc.value.status_code == 409
    assert "no credit approval yet" in str(exc.value.detail)


# ── Send-offer readiness ─────────────────────────────────────────────────────

def test_readiness_lists_every_blocker() -> None:
    r = offer_flow._readiness(FakeConn(DEAL), IID, PID)
    assert r["ready"] is False and len(r["blockers"]) == 3
    assert r["default_template_id"] is None


def test_readiness_ready_with_defaults() -> None:
    conn = FakeConn({
        **DEAL,
        "FROM institution.approval_instance": [
            {"id": "i", "status": "approved", "resolved_at": None}],
        "LEFT JOIN marketplace.bid_acceptance": [
            {"full_name": "Jane Doe", "email": "jane@x.mu", "product_label": "Home Loan",
             "consumer_ref": "8F2A1C3D"}],
        "FROM institution.doc_template": [
            {"id": "t1", "name": "Facility letter", "doc_category": "facility_letter",
             "current_version": 1}],
        "FROM institution.esign_envelope": [{"n": 0}],
    })
    r = offer_flow._readiness(conn, IID, PID)
    assert r["ready"] is True and r["blockers"] == []
    assert r["default_template_id"] == "t1"
    assert r["borrower"] == {"name": "Jane Doe", "email": "jane@x.mu"}
    assert r["deal_label"] == "Home Loan — ref 8F2A1C3D"


def test_readiness_unknown_deal_404() -> None:
    with pytest.raises(HTTPException) as exc:
        offer_flow._readiness(FakeConn({}), IID, PID)
    assert exc.value.status_code == 404


# ── Setup checklist ──────────────────────────────────────────────────────────

def test_setup_fresh_institution_offers_install() -> None:
    s = offer_setup.setup_status(FakeConn({}), IID)
    assert s["complete"] is False and s["can_install"] is True
    assert [i["key"] for i in s["items"]] == [
        "second_person", "checker", "credit_chain", "offer_template"]


def test_setup_waiting_on_second_admin() -> None:
    conn = FakeConn({
        "FROM institution.member WHERE": [{"n": 2, "checkers": 1}],
        "FROM institution.doa_rule r": [{"status": "pending_activation"}],
        "FROM institution.doc_template t": [{"live": False, "waiting": True}],
    })
    s = offer_setup.setup_status(conn, IID)
    states = {i["key"]: i["state"] for i in s["items"]}
    assert states == {"second_person": "done", "checker": "done",
                      "credit_chain": "waiting", "offer_template": "waiting"}
    assert s["can_install"] is False


# ── Upload-time merge-tag validation ─────────────────────────────────────────

def _docx(tmp_path: Path, body: str) -> Path:
    d = Document()
    d.add_paragraph(body)
    p = tmp_path / "t.docx"
    d.save(p)
    return p


def test_typo_gets_suggestion(tmp_path: Path) -> None:
    c = merge_engine.check_template(_docx(tmp_path, "Dear {{ borower_full_name }}"),
                                    dt_router.known_merge_fields())
    assert not c.ok
    assert "did you mean {{ borrower_full_name }}" in c.message()


def test_broken_tag_explained(tmp_path: Path) -> None:
    c = merge_engine.check_template(_docx(tmp_path, "Dear {{ borrower_full_name }"),
                                    dt_router.known_merge_fields())
    assert not c.ok and "{{ field_name }}" in c.message()


def test_starter_template_is_valid(tmp_path: Path) -> None:
    p = tmp_path / "s.docx"
    p.write_bytes(starter.build_starter_docx())
    c = merge_engine.check_template(p, dt_router.known_merge_fields())
    assert c.ok and "borrower_full_name" in c.used


async def test_bad_upload_refused_before_storage(monkeypatch, tmp_path: Path) -> None:
    async def _no_upload(*_a: Any) -> None:
        raise AssertionError("must not store an invalid template")

    monkeypatch.setattr(dt_router, "_storage_upload", _no_upload)
    data = _docx(tmp_path, "{{ nope }}").read_bytes()
    with pytest.raises(HTTPException) as exc:
        await dt_router.store_version(
            FakeConn({}), claims={}, institution_id=IID,
            template={"id": "t", "current_version": 0}, creator_id="m",
            file_name="t.docx", data=data, change_note=None,
        )
    assert exc.value.status_code == 422 and "{{ nope }}" in str(exc.value.detail)
