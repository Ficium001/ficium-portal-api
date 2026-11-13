"""Institution applications (/access-requests): a record only, confirmed by email.

Applying must never create an institution, a login or a member; must answer the
same whatever the email's history (no enumeration); and must store the link
token only as a hash.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import access_requests as ar
from app.core.mail import EmailNotSent
from app.core.ratelimit import limiter
from app.main import app

GOOD = {
    "institution_name": "ZZ Bank", "institution_type": "commercial_bank", "country": "mu",
    "contact_name": "Ada Lovelace", "contact_email": " Ada@ZZBank.mu ",
    "regulator": "Bank of Mauritius", "website": "https://zzbank.mu",
}


class Svc:
    def __init__(self, open_row: Any = None, verify_hash: str | None = None) -> None:
        self.open_row = open_row
        self.verify_hash = verify_hash
        self.sql: list[tuple[str, dict[str, Any]]] = []

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        q, p = " ".join(str(stmt).split()), params or {}
        self.sql.append((q, p))
        row = None
        if "FROM institution.access_request" in q and "FOR UPDATE" in q:
            row = self.open_row
        elif "SET status = 'submitted'" in q:
            row = (SimpleNamespace(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                                   reference="AB12CD34EF", institution_name="ZZ Bank")
                   if p.get("h") == self.verify_hash else None)
        return SimpleNamespace(fetchone=lambda: row)

    def writes(self) -> list[str]:
        return [q.split(" (")[0].split(" SET")[0] for q, _ in self.sql
                if q.startswith(("INSERT", "UPDATE", "DELETE"))]


@pytest.fixture(autouse=True)
def _reset_limits() -> Any:
    limiter.reset()
    yield
    limiter.reset()


def _call(
    svc: Svc, path: str, body: dict[str, Any], mail: AsyncMock | None = None,
) -> tuple[Any, AsyncMock]:
    mail = mail or AsyncMock()

    @contextmanager
    def _session():  # noqa: ANN202
        yield svc

    with patch.object(ar, "service_session", _session), patch.object(ar, "send_email", mail):
        r = TestClient(app, raise_server_exceptions=False).post(path, json=body)
    return r, mail


def _token_from(mail: AsyncMock) -> str:
    html = mail.await_args.args[2]
    return re.search(r"token=([A-Za-z0-9_\-]+)", html).group(1)


def test_new_application_is_a_record_only_and_emails_a_hashed_link() -> None:
    svc = Svc()
    r, mail = _call(svc, "/access-requests", GOOD)
    assert r.status_code == 202 and r.json() == ar.RECEIVED
    assert svc.writes() == ["INSERT INTO institution.access_request", "INSERT INTO audit.event"]
    assert not any("institution.institution" in q or "auth_portal" in q or "institution.member" in q
                   for q, _ in svc.sql)
    ins = next(p for q, p in svc.sql if q.startswith("INSERT INTO institution.access_request"))
    assert ins["email"] == "ada@zzbank.mu" and ins["country"] == "MU"
    token = _token_from(mail)
    assert ins["h"] == hashlib.sha256(token.encode()).hexdigest() and token not in str(ins)
    assert mail.await_args.args[0] == "ada@zzbank.mu"
    assert "/request-access/verify?token=" in mail.await_args.args[2]


def test_honeypot_gets_the_same_answer_and_nothing_happens() -> None:
    svc = Svc()
    r, mail = _call(svc, "/access-requests", {**GOOD, "company_fax": "+230 000"})
    assert r.status_code == 202 and r.json() == ar.RECEIVED
    assert svc.sql == [] and mail.await_count == 0


def test_resubmitting_before_confirming_replaces_the_link_not_the_row() -> None:
    svc = Svc(open_row=SimpleNamespace(id="x", status="unverified", link_expired=False))
    r, mail = _call(svc, "/access-requests", GOOD)
    assert r.json() == ar.RECEIVED and mail.await_count == 1
    assert svc.writes() == ["UPDATE institution.access_request"]


def test_an_application_already_with_staff_gets_the_same_answer_and_no_mail() -> None:
    svc = Svc(open_row=SimpleNamespace(id="x", status="in_review", link_expired=None))
    r, mail = _call(svc, "/access-requests", GOOD)
    assert r.status_code == 202 and r.json() == ar.RECEIVED
    assert svc.writes() == [] and mail.await_count == 0


def test_an_expired_unconfirmed_one_is_closed_and_a_new_one_opened() -> None:
    svc = Svc(open_row=SimpleNamespace(id="x", status="unverified", link_expired=True))
    _, mail = _call(svc, "/access-requests", GOOD)
    assert svc.writes() == [
        "UPDATE institution.access_request",
        "INSERT INTO institution.access_request",
        "INSERT INTO audit.event",
    ]
    assert "status = 'expired'" in next(q for q, _ in svc.sql if q.startswith("UPDATE"))
    assert mail.await_count == 1


def test_a_mail_failure_does_not_change_the_answer() -> None:
    r, _ = _call(Svc(), "/access-requests", GOOD, mail=AsyncMock(side_effect=EmailNotSent("down")))
    assert r.status_code == 202 and r.json() == ar.RECEIVED


@pytest.mark.parametrize("field,value", [
    ("contact_email", "not-an-email"),
    ("institution_type", "casino"),
    ("website", "zzbank.mu"),
    ("institution_name", " "),
    ("country", "M1"),
])
def test_invalid_fields_rejected_before_any_write(field: str, value: str) -> None:
    svc = Svc()
    r, mail = _call(svc, "/access-requests", {**GOOD, field: value})
    assert r.status_code == 422 and svc.sql == [] and mail.await_count == 0


def test_rate_limited_per_ip() -> None:
    codes = [_call(Svc(), "/access-requests", GOOD)[0].status_code for _ in range(6)]
    assert codes[:5] == [202] * 5 and codes[5] == 429


def test_the_link_confirms_once_and_returns_a_reference() -> None:
    token = "t" * 43
    svc = Svc(verify_hash=hashlib.sha256(token.encode()).hexdigest())
    r, _ = _call(svc, "/access-requests/verify", {"token": token})
    assert r.status_code == 200
    assert r.json() == {
        "status": "submitted", "reference": "AB12CD34EF", "institution_name": "ZZ Bank",
    }
    upd = next(q for q, _ in svc.sql if q.startswith("UPDATE"))
    assert "status = 'unverified'" in upd and "verify_expires_at > now()" in upd
    assert "verify_token_hash = NULL" in upd


def test_a_wrong_or_used_link_is_refused() -> None:
    svc = Svc(verify_hash="something-else")
    r, _ = _call(svc, "/access-requests/verify", {"token": "u" * 43})
    assert r.status_code == 400 and "expired" in r.json()["detail"]
    assert not any(q.startswith("INSERT INTO audit.event") for q, _ in svc.sql)
