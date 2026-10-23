"""POST /onboarding/submit: preferences saved and stage advanced in one
transaction, only when every required document is uploaded (db/049 rule)."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.deps import current_claims
from app.main import app

IID, SUB = str(uuid.uuid4()), str(uuid.uuid4())
PREFS = {"deployment_model": "saas", "integration_mode": "api", "it_contact_name": "IT",
         "it_contact_email": "IT@zz.mu"}


class Svc:
    def __init__(self, *, stage: str = "registered", uploaded: tuple[bool, ...] = (True, True),
                 member: bool = True) -> None:
        self.stage, self.uploaded, self.member = stage, uploaded, member
        self.sql: list[tuple[str, dict[str, Any]]] = []

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        q = " ".join(str(stmt).split())
        self.sql.append((q, params or {}))
        if "SELECT onboarding_stage" in q:
            return SimpleNamespace(fetchone=lambda: SimpleNamespace(onboarding_stage=self.stage))
        if "FROM institution.member" in q:
            return SimpleNamespace(fetchone=lambda: (1,) if self.member else None)
        if "required_doc_types" in q:
            rows = [SimpleNamespace(id=uuid.uuid4(), code=f"d{i}", label=f"Doc {i}",
                                    description=None, uploaded=u)
                    for i, u in enumerate(self.uploaded)]
            return SimpleNamespace(fetchall=lambda: rows)
        return SimpleNamespace(fetchone=lambda: None)

    def writes(self) -> list[str]:
        return [q.split(" (")[0].split(" SET")[0] for q, _ in self.sql
                if q.startswith(("INSERT", "UPDATE"))]


def _post(svc: Svc, role: str = "institution_admin", body: dict | None = None) -> Any:
    async def claims() -> dict[str, Any]:
        return {"sub": SUB, "institution_id": IID, "user_role": role}

    @contextmanager
    def _session():  # noqa: ANN202
        yield svc

    app.dependency_overrides[current_claims] = claims
    try:
        with patch("app.api.onboarding.service_session", _session):
            return TestClient(app, raise_server_exceptions=False).post(
                "/onboarding/submit", json=body or PREFS)
    finally:
        app.dependency_overrides.clear()


def test_complete_submission_saves_prefs_and_advances_stage_together() -> None:
    svc = Svc()
    r = _post(svc)
    assert r.status_code == 200, r.text
    assert svc.writes() == ["INSERT INTO institution.onboarding_pref",
                            "UPDATE institution.institution", "INSERT INTO audit.event"]
    assert any("'commercial_review'" in q for q, _ in svc.sql)


def test_missing_documents_block_submission_and_nothing_is_written() -> None:
    svc = Svc(uploaded=(True, False))
    r = _post(svc)
    assert r.status_code == 422 and "Doc 1" in r.json()["detail"]
    assert svc.writes() == []


def test_only_the_institution_admin_can_submit() -> None:
    svc = Svc()
    assert _post(svc, role="maker").status_code == 403
    assert svc.sql == []


def test_cannot_submit_twice() -> None:
    svc = Svc(stage="commercial_review")
    assert _post(svc).status_code == 409
    assert svc.writes() == []


@pytest.mark.parametrize("field,value", [("deployment_model", "cloud"),
                                         ("webhook_url", "http://insecure.example"),
                                         ("it_contact_email", "nope")])
def test_invalid_preferences_rejected(field: str, value: str) -> None:
    svc = Svc()
    assert _post(svc, body={**PREFS, field: value}).status_code == 422
    assert svc.sql == []
