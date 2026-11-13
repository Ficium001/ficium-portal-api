"""
Institution applications: the ONLY public way a financial institution asks to join.

    POST /access-requests          submit (record only, 202, same answer every time)
    POST /access-requests/verify   confirm with the emailed link

KJ (2026-11-13): the portal is restrictive, SaaS-like. Applying creates NO
institution, NO login and NO member. It stores the application as 'unverified'
and emails a single-use confirmation link (24 h) to the contact address. Only a
confirmed application ('submitted') reaches Ficium staff on the admin site. After
the contract is signed, two admins provision it (internal_admin institution.provision).

Abuse: 5 submissions/hour/IP, a honeypot field, the email round-trip (so staff
never see unconfirmed spam), and one open application per email. The submit
answer is identical whether the email is new, already applying or invalid, so the
endpoint cannot be used to learn who has applied. The link token is stored only as
a SHA-256 hash.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import secrets
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from ..core.config import settings
from ..core.db import service_session
from ..core.mail import EmailNotSent, send_email
from ..core.ratelimit import limiter

log = structlog.get_logger()
router = APIRouter(prefix="/access-requests", tags=["access-requests"])

INSTITUTION_TYPES = frozenset(
    {"commercial_bank", "fintech", "micro_credit", "leasing", "insurance", "cooperative", "other"}
)
OPEN_STATUSES = ("unverified", "submitted", "in_review", "info_requested", "approved")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")

RECEIVED = {
    "status": "received",
    "message": "If the details are valid, we have emailed a link to confirm your application. "
               "It expires in 24 hours.",
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _opt(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


class AccessRequestBody(BaseModel):
    institution_name: str = Field(min_length=2, max_length=200)
    institution_type: str = Field(default="commercial_bank")
    country:          str = Field(default="MUS", min_length=2, max_length=3, pattern=r"^[A-Za-z]+$")
    reg_number:       str | None = Field(default=None, max_length=100)
    regulator:        str | None = Field(default=None, max_length=100)
    website:          str | None = Field(default=None, max_length=300)
    contact_name:     str = Field(min_length=2, max_length=200)
    contact_email:    str = Field(max_length=254)
    contact_phone:    str | None = Field(default=None, max_length=40)
    contact_role:     str | None = Field(default=None, max_length=100)
    message:          str | None = Field(default=None, max_length=2000)
    # Honeypot: hidden in the form. People leave it empty; form-filling bots don't.
    company_fax:      str | None = Field(default=None, max_length=200)

    @field_validator("institution_type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in INSTITUTION_TYPES:
            raise ValueError(f"institution_type must be one of {sorted(INSTITUTION_TYPES)}")
        return v

    @field_validator("contact_email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        return v.upper()

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        v = _opt(v)
        if v and not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("Website must start with http:// or https://")
        return v

    @field_validator("institution_name", "contact_name")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Too short")
        return v


def _audit(conn: Any, action: str, request_id: str, meta: dict[str, Any], ip: str | None) -> None:
    conn.execute(
        text("""
            INSERT INTO audit.event (actor_type, actor_ip, action, resource_type, resource_id,
                                     outcome, metadata)
            VALUES ('public', CAST(:ip AS inet), :action, 'access_request', CAST(:rid AS uuid),
                    'success', CAST(:meta AS jsonb))
        """),
        {"ip": ip, "action": action, "rid": request_id, "meta": _json(meta)},
    )


def _json(d: dict[str, Any]) -> str:
    return json.dumps(d, default=str)


def _client_ip(request: Request) -> str | None:
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def _confirm_email(name: str, url: str) -> str:
    n = html.escape(name)
    u = html.escape(url, quote=True)
    return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;color:#0A0A1A;padding:32px 16px">
<div style="max-width:480px;margin:0 auto">
  <h2 style="margin:0 0 12px">Confirm your application to Ficium</h2>
  <p style="color:#4a4a63;line-height:1.6">We received an application for <strong>{n}</strong> to join
  the Ficium institution portal. Confirm it with the button below so our team can review it.
  The link expires in 24 hours and works once.</p>
  <p><a href="{u}" style="display:inline-block;background:#2A1FE6;color:#fff;padding:12px 24px;
  border-radius:8px;text-decoration:none;font-weight:600">Confirm application</a></p>
  <p style="color:#6B6B85;font-size:13px">Applying does not create an account. If the application
  is accepted and a contract is signed, your administrator receives a separate invitation.</p>
  <p style="color:#6B6B85;font-size:13px">Not you? Ignore this email; nothing happens.</p>
</div></body></html>"""


@router.post("", status_code=202)
@limiter.limit("5/hour")
async def submit(request: Request, response: Response, body: AccessRequestBody) -> dict[str, Any]:
    if body.company_fax:
        log.info("access_request_honeypot")
        return RECEIVED

    ip = _client_ip(request)
    raw = secrets.token_urlsafe(32)
    expires = settings.access_request_verify_ttl
    send_to: str | None = None

    with service_session() as conn:
        open_row = conn.execute(
            text("""
                SELECT id, status, verify_expires_at < now() AS link_expired
                  FROM institution.access_request
                 WHERE lower(contact_email) = :e AND status = ANY(CAST(:open AS text[]))
                 FOR UPDATE
            """),
            {"e": body.contact_email, "open": list(OPEN_STATUSES)},
        ).fetchone()

        if open_row is not None and open_row.status == "unverified" and not open_row.link_expired:
            # Same person trying again before confirming: replace the link, keep one row.
            conn.execute(
                text("""
                    UPDATE institution.access_request
                       SET verify_token_hash = :h,
                           verify_expires_at = now() + make_interval(secs => :ttl),
                           updated_at = now()
                     WHERE id = :id
                """),
                {"h": token_hash(raw), "ttl": expires, "id": open_row.id},
            )
            send_to = body.contact_email
        elif open_row is not None and open_row.status != "unverified":
            # Already confirmed and with staff: say nothing more (no enumeration, no spam).
            pass
        else:
            if open_row is not None:   # an unconfirmed link that ran out
                conn.execute(
                    text("""UPDATE institution.access_request
                               SET status = 'expired', verify_token_hash = NULL, updated_at = now()
                             WHERE id = :id"""),
                    {"id": open_row.id},
                )
            rid = str(uuid.uuid4())
            conn.execute(
                text("""
                    INSERT INTO institution.access_request
                      (id, institution_name, institution_type, country, reg_number, regulator,
                       website, contact_name, contact_email, contact_phone, contact_role, message,
                       verify_token_hash, verify_expires_at, submit_ip)
                    VALUES
                      (CAST(:id AS uuid), :name, :type, :country, :reg, :regulator,
                       :website, :cname, :email, :phone, :role, :message,
                       :h, now() + make_interval(secs => :ttl), CAST(:ip AS inet))
                """),
                {"id": rid, "name": body.institution_name, "type": body.institution_type,
                 "country": body.country, "reg": _opt(body.reg_number),
                 "regulator": _opt(body.regulator), "website": body.website,
                 "cname": body.contact_name, "email": body.contact_email,
                 "phone": _opt(body.contact_phone), "role": _opt(body.contact_role),
                 "message": _opt(body.message), "h": token_hash(raw), "ttl": expires, "ip": ip},
            )
            _audit(conn, "access_request.received", rid,
                   {"institution_name": body.institution_name, "type": body.institution_type}, ip)
            send_to = body.contact_email

    if send_to:
        url = f"{settings.portal_public_url.rstrip('/')}/request-access/verify?token={raw}"
        try:
            await send_email(send_to, "Confirm your application to Ficium",
                             _confirm_email(body.institution_name, url))
        except EmailNotSent:
            # Same answer either way; the applicant can submit again to get a new link.
            log.error("access_request_confirm_email_failed")
    return RECEIVED


class VerifyBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)


@router.post("/verify")
@limiter.limit("20/hour")
async def verify(request: Request, response: Response, body: VerifyBody) -> dict[str, Any]:
    with service_session() as conn:
        row = conn.execute(
            text("""
                UPDATE institution.access_request
                   SET status = 'submitted', verified_at = now(),
                       verify_token_hash = NULL, verify_expires_at = NULL, updated_at = now()
                 WHERE verify_token_hash = :h AND status = 'unverified'
                   AND verify_expires_at > now()
                RETURNING id, reference, institution_name
            """),
            {"h": token_hash(body.token)},
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=400,
                detail="This link is invalid or has expired. Submit the application again.",
            )
        _audit(conn, "access_request.submitted", str(row.id), {"reference": row.reference},
               _client_ip(request))

    if settings.access_request_notify_email:
        try:
            await send_email(
                settings.access_request_notify_email,
                f"New institution application {row.reference}",
                f"<p>{html.escape(row.institution_name)} confirmed an application "
                f"(reference {html.escape(row.reference)}). Review it on the portal admin site.</p>",
            )
        except EmailNotSent:
            log.error("access_request_notify_failed", reference=row.reference)

    return {"status": "submitted", "reference": row.reference,
            "institution_name": row.institution_name}
