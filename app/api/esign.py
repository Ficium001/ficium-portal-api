# =============================================================================
# ficium-portal-api — E-Signature router (inst:esign)
#
# Envelope lifecycle for offer letters / mandates, gated on an APPROVED
# approval-engine instance (enforced in SQL — db/008_esign.sql).
#
# Two surfaces:
#   /esign/*          institution side (JWT, tenant_conn)
#   /esign/public/*   signer ceremony (NO JWT — auth is the unguessable
#                     signer token, stored as SHA-256 only; uses
#                     service_session because there is no tenant context
#                     before the token resolves; every state transition
#                     still runs through SECURITY DEFINER functions).
#
# Email: Resend if RESEND_API_KEY is set; otherwise the signing link / OTP
# is logged (structlog) so dev + staging work without a mail provider.
# Storage: Supabase Storage REST, same pattern as documents.py
# (PORTAL_SUPABASE_URL + PORTAL_SUPABASE_SERVICE_KEY), bucket
# `institution-docs` under esign/.
# =============================================================================

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from ..core.db import service_session
from ..core.storage import storage_download as _storage_download
from ..core.storage import storage_signed_url as _storage_signed_url
from ..core.storage import storage_upload as _storage_upload
from ..deps import current_claims as get_claims
from ..deps import tenant_conn


def _row_or_500(row: Row[Any] | None, context: str) -> Row[Any]:
    """Narrow Row | None from SQL function calls that must return a row."""
    if row is None:
        log.error("esign_missing_row", context=context)
        raise HTTPException(status_code=500, detail="Internal error.")
    return row


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


def service_conn():
    """FastAPI dependency wrapper over core.db.service_session (privileged,
    outside tenant RLS — used ONLY for signer-token resolution where no
    tenant context exists before the token resolves)."""
    with service_session() as session:
        yield session

log = structlog.get_logger()

router = APIRouter(prefix="/esign", tags=["esign"])

MODULE_KEY = "inst:esign"
OTP_TTL_MIN = 10
OTP_MAX_ATTEMPTS = 5


# ── helpers ──────────────────────────────────────────────────────────────────

def _require_module(claims: dict) -> None:
    role = claims.get("user_role", "")
    if role in ("super_admin", "institution_super_admin"):
        return
    if MODULE_KEY not in claims.get("module_permissions", []):
        raise HTTPException(status_code=403, detail=f"Requires '{MODULE_KEY}'.")


def _row(r) -> dict:
    return dict(r._mapping)


def _otp_pepper() -> bytes:
    pepper = os.environ.get("ESIGN_OTP_PEPPER", "")
    if not pepper:
        raise HTTPException(status_code=500, detail="ESIGN_OTP_PEPPER not configured.")
    return pepper.encode()


def _otp_hash(otp: str) -> str:
    return hmac.new(_otp_pepper(), otp.encode(), hashlib.sha256).hexdigest()


def _new_token() -> tuple[str, str]:
    raw = f"fic_sign_{secrets.token_hex(32)}"
    return raw, hashlib.sha256(raw.encode()).hexdigest()


async def _send_email(to: str, subject: str, html: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning("esign_email_dev_mode", to=to, subject=subject, body=html)
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": os.environ.get("ESIGN_EMAIL_FROM", "Ficium <no-reply@ficium.net>"),
                "to": [to], "subject": subject, "html": html,
            },
        )
    if resp.status_code not in (200, 201):
        log.error("esign_email_failed", to=to, status=resp.status_code)


# ── institution side ─────────────────────────────────────────────────────────

@router.post("/envelopes")
async def create_envelope(
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    """
    body: { entity_type, entity_id, approval_instance_id, title, document_path,
            expires_hours?, borrower_name, borrower_email, borrower_ref?,
            countersigner_name, countersigner_email, countersigner_ref }
    Approval gate enforced in institution.esign_create_envelope().
    """
    _require_module(claims)
    doc = await _storage_download(body["document_path"])
    doc_hash = hashlib.sha256(doc).hexdigest()

    b_raw, b_hash = _new_token()
    c_raw, c_hash = _new_token()
    signers = [
        {"party": "borrower", "sign_order": 1,
         "display_name": body["borrower_name"], "email": body["borrower_email"],
         "user_ref": str(body.get("borrower_ref") or ""), "token_sha256": b_hash},
        {"party": "institution", "sign_order": 2,
         "display_name": body["countersigner_name"], "email": body["countersigner_email"],
         "user_ref": str(body["countersigner_ref"]), "token_sha256": c_hash},
    ]
    try:
        row = conn.execute(text("""
            SELECT institution.esign_create_envelope(
              :iid, :entity_type, :entity_id, :approval_instance, :title,
              :document_path, :doc_hash,
              now() + make_interval(hours => :expires_hours), :uid,
              CAST(:signers AS jsonb)) AS id
        """), {
            "iid": claims["institution_id"], "uid": claims["sub"],
            "entity_type": body["entity_type"], "entity_id": body["entity_id"],
            "approval_instance": body.get("approval_instance_id"),
            "title": body["title"], "document_path": body["document_path"],
            "doc_hash": doc_hash,
            "expires_hours": int(body.get("expires_hours") or 72),
            "signers": json.dumps(signers),
        }).fetchone()
    except Exception as e:  # noqa: BLE001
        if "ESIGN_APPROVAL_NOT_GRANTED" in str(e):
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="This document requires a completed approval first.",
            ) from e
        raise
    row = _row_or_500(row, "esign_create_envelope")
    env_id = str(row.id)
    conn.execute(text(
        "UPDATE institution.esign_envelope SET status = 'sent' WHERE id = :id"
    ), {"id": env_id})
    conn.execute(text(
        "SELECT institution.esign_append_event(:id, NULL, 'sent', CAST(:d AS jsonb))"
    ), {"id": env_id, "d": json.dumps({"to": body["borrower_email"]})})
    conn.commit()

    base = os.environ.get("CLIENT_APP_URL", "https://app.ficium.net")
    ttl = int(body.get("expires_hours") or 72)
    await _send_email(
        body["borrower_email"], f"Action required: sign {body['title']}",
        f"<p>Dear {body['borrower_name']},</p>"
        f"<p>Please review and sign: <a href='{base}/sign/{b_raw}'>{body['title']}</a>."
        f" This link expires in {ttl} hours.</p>",
    )
    await _send_email(
        body["countersigner_email"], f"Countersignature pending: {body['title']}",
        f"<p>Dear {body['countersigner_name']},</p>"
        f"<p>Your countersignature will be required once the borrower signs: "
        f"<a href='{base}/sign/{c_raw}'>{body['title']}</a>.</p>",
    )
    return {"envelope_id": env_id}


@router.get("/envelopes")
async def list_envelopes(
    conn: Session = Depends(tenant_conn), claims: dict = Depends(get_claims)
) -> list[dict]:
    _require_module(claims)
    rows = conn.execute(text("""
        SELECT e.id, e.title, e.entity_type, e.entity_id, e.status,
               e.expires_at, e.completed_at, e.sealed_path, e.sealed_sha256,
               COALESCE(jsonb_agg(jsonb_build_object(
                 'party', s.party, 'display_name', s.display_name,
                 'status', s.status, 'signed_at', s.signed_at)
                 ORDER BY s.sign_order), '[]') AS signers
        FROM institution.esign_envelope e
        LEFT JOIN institution.esign_signer s ON s.envelope_id = e.id
        GROUP BY e.id ORDER BY e.created_at DESC
    """)).fetchall()
    return [_row(r) for r in rows]


@router.get("/envelopes/{envelope_id}/events")
async def envelope_events(
    envelope_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_module(claims)
    rows = conn.execute(text("""
        SELECT event, detail, ip, created_at, prev_hash, hash
        FROM institution.esign_event WHERE envelope_id = :id ORDER BY id
    """), {"id": envelope_id}).fetchall()
    prev, intact = "GENESIS", True
    for r in rows:
        if r.prev_hash != prev:
            intact = False
            break
        prev = r.hash
    return {"chain_intact": intact, "events": [_row(r) for r in rows]}


@router.get("/envelopes/{envelope_id}/sealed-url")
async def sealed_url(
    envelope_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(get_claims),
) -> dict:
    _require_module(claims)
    row = conn.execute(text("""
        SELECT sealed_path, sealed_sha256 FROM institution.esign_envelope
        WHERE id = :id AND status = 'completed'
    """), {"id": envelope_id}).fetchone()
    if row is None or not row.sealed_path:
        raise HTTPException(status_code=404, detail="Sealed document not available.")
    url = await _storage_signed_url(row.sealed_path)
    return {"url": url, "sha256": row.sealed_sha256}


# ── public signer ceremony (token auth, no JWT) ──────────────────────────────

def _signer_by_token(conn: Session, raw_token: str):
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return conn.execute(text("""
        SELECT sg.id, sg.envelope_id, sg.party, sg.sign_order, sg.display_name,
               sg.status, sg.otp_verified_at, sg.email,
               e.title, e.status AS envelope_status, e.expires_at,
               e.document_path, e.institution_id
        FROM institution.esign_signer sg
        JOIN institution.esign_envelope e ON e.id = sg.envelope_id
        WHERE sg.token_sha256 = :h
    """), {"h": token_hash}).fetchone()


def _live_signer(conn: Session, token: str):
    s = _signer_by_token(conn, token)
    if s is None:
        raise HTTPException(status_code=404, detail="Invalid or expired signing link.")
    if s.envelope_status in ("voided", "expired", "declined"):
        raise HTTPException(status_code=410, detail=f"Envelope is {s.envelope_status}.")
    return s


@router.get("/public/{token}")
async def ceremony_state(
    token: str, request: Request, conn: Session = Depends(service_conn)
) -> dict:
    s = _live_signer(conn, token)
    if s.status == "pending":
        conn.execute(text(
            "UPDATE institution.esign_signer SET status = 'viewed' WHERE id = :id"
        ), {"id": s.id})
    conn.execute(text(
        "SELECT institution.esign_append_event(:e, :s, 'viewed', '{}', "
        "CAST(:ip AS inet), :ua)"
    ), {"e": s.envelope_id, "s": s.id,
        "ip": _client_ip(request), "ua": request.headers.get("user-agent", "")})
    conn.commit()
    doc_url = await _storage_signed_url(s.document_path)
    return {
        "title": s.title, "party": s.party, "display_name": s.display_name,
        "signer_status": s.status, "envelope_status": s.envelope_status,
        "expires_at": s.expires_at.isoformat(), "document_url": doc_url,
        "otp_verified": s.otp_verified_at is not None,
    }


@router.post("/public/{token}/otp")
async def request_otp(token: str, conn: Session = Depends(service_conn)) -> dict:
    s = _live_signer(conn, token)
    if s.status == "signed":
        raise HTTPException(status_code=409, detail="Already signed.")
    otp = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(text("""
        UPDATE institution.esign_signer
        SET otp_hash = :h, otp_expires_at = now() + make_interval(mins => :ttl),
            otp_attempts = 0, otp_verified_at = NULL
        WHERE id = :id
    """), {"id": s.id, "h": _otp_hash(otp), "ttl": OTP_TTL_MIN})
    conn.execute(text(
        "SELECT institution.esign_append_event(:e, :s, 'otp_sent', '{}')"
    ), {"e": s.envelope_id, "s": s.id})
    conn.commit()
    await _send_email(
        s.email, "Your Ficium signing code",
        f"<p>Dear {s.display_name},</p><p>Your one-time signing code is "
        f"<strong>{otp}</strong>. It expires in {OTP_TTL_MIN} minutes.</p>",
    )
    return {"ok": True}


@router.post("/public/{token}/otp/verify")
async def verify_otp(
    token: str, request: Request,
    body: dict = Body(...), conn: Session = Depends(service_conn),
) -> dict:
    s = _live_signer(conn, token)
    otp = (body.get("otp") or "").strip()
    row = conn.execute(text("""
        SELECT otp_hash, otp_expires_at, otp_attempts
        FROM institution.esign_signer WHERE id = :id FOR UPDATE
    """), {"id": s.id}).fetchone()
    valid = (
        row is not None and row.otp_hash is not None
        and row.otp_attempts < OTP_MAX_ATTEMPTS
        and bool(getattr(conn.execute(text("SELECT :e > now() AS ok"),
                                      {"e": row.otp_expires_at}).fetchone(),
                         "ok", False))
        and hmac.compare_digest(row.otp_hash, _otp_hash(otp))
    )
    ip, ua = _client_ip(request), request.headers.get("user-agent", "")
    if valid:
        conn.execute(text("""
            UPDATE institution.esign_signer
            SET otp_verified_at = now(), otp_hash = NULL WHERE id = :id
        """), {"id": s.id})
        conn.execute(text(
            "SELECT institution.esign_append_event(:e, :s, 'otp_verified', '{}', "
            "CAST(:ip AS inet), :ua)"
        ), {"e": s.envelope_id, "s": s.id, "ip": ip, "ua": ua})
        conn.commit()
        return {"ok": True}
    conn.execute(text(
        "UPDATE institution.esign_signer SET otp_attempts = otp_attempts + 1 "
        "WHERE id = :id"
    ), {"id": s.id})
    conn.execute(text(
        "SELECT institution.esign_append_event(:e, :s, 'otp_failed', '{}', "
        "CAST(:ip AS inet), :ua)"
    ), {"e": s.envelope_id, "s": s.id, "ip": ip, "ua": ua})
    conn.commit()
    raise HTTPException(status_code=401, detail="Invalid or expired code.")


@router.post("/public/{token}/sign")
async def sign(
    token: str, request: Request, conn: Session = Depends(service_conn)
) -> dict:
    s = _live_signer(conn, token)
    try:
        row = conn.execute(text(
            "SELECT institution.esign_sign(:id, CAST(:ip AS inet), :ua) AS status"
        ), {"id": s.id, "ip": _client_ip(request),
            "ua": request.headers.get("user-agent", "")}).fetchone()
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        conn.rollback()
        for code, (status, detail) in {
            "ESIGN_OTP_NOT_VERIFIED": (403, "Verify the code sent to your email first."),
            "ESIGN_OUT_OF_ORDER": (409, "Awaiting a prior signature."),
            "ESIGN_ENVELOPE_EXPIRED": (410, "This envelope has expired."),
            "ESIGN_ALREADY_SIGNED": (409, "You have already signed."),
            "ESIGN_ENVELOPE_NOT_SIGNABLE": (409, "Envelope is not open for signing."),
        }.items():
            if code in msg:
                raise HTTPException(status_code=status, detail=detail) from e
        raise
    row = _row_or_500(row, "esign_sign")
    status = row.status
    conn.commit()
    if status == "completed":
        await _seal(conn, str(s.envelope_id))
    return {"envelope_status": status}


@router.post("/public/{token}/decline")
async def decline(
    token: str, request: Request,
    body: dict = Body(...), conn: Session = Depends(service_conn),
) -> dict:
    s = _live_signer(conn, token)
    reason = (body.get("reason") or "").strip()
    if len(reason) < 3:
        raise HTTPException(status_code=422, detail="A brief reason is required.")
    conn.execute(text(
        "SELECT institution.esign_decline(:id, :reason, CAST(:ip AS inet), :ua)"
    ), {"id": s.id, "reason": reason, "ip": _client_ip(request),
        "ua": request.headers.get("user-agent", "")})
    conn.commit()
    return {"ok": True}


# ── sealing ──────────────────────────────────────────────────────────────────

async def _seal(conn: Session, envelope_id: str) -> None:
    """Append the audit certificate page and store the sealed PDF + its hash."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    env = _row_or_500(conn.execute(text(
        "SELECT * FROM institution.esign_envelope WHERE id = :id"
    ), {"id": envelope_id}).fetchone(), "esign_seal_envelope")
    signers = conn.execute(text("""
        SELECT sign_order, display_name, email, party, signed_at, signed_ip
        FROM institution.esign_signer WHERE envelope_id = :id ORDER BY sign_order
    """), {"id": envelope_id}).fetchall()
    events = conn.execute(text("""
        SELECT event, created_at, hash FROM institution.esign_event
        WHERE envelope_id = :id ORDER BY id
    """), {"id": envelope_id}).fetchall()
    chain_tip = events[-1].hash if events else "GENESIS"

    source = await _storage_download(env.document_path)
    if hashlib.sha256(source).hexdigest() != env.document_sha256:
        log.error("esign_source_tampered", envelope_id=envelope_id)
        raise HTTPException(status_code=409, detail="Source document integrity failure.")

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, h = A4
    y = h - 25 * mm

    def line(t: str, size: int = 9, dy: float = 5.2 * mm, bold: bool = False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(20 * mm, y, t[:110])
        y -= dy

    line("Ficium — Certificate of completion", 14, 8 * mm, bold=True)
    line(f"Envelope: {env.id}")
    line(f"Title: {env.title}")
    line(f"Source document SHA-256: {env.document_sha256}")
    line(f"Completed at (UTC): {env.completed_at}")
    y -= 3 * mm
    line("Signers", 11, 7 * mm, bold=True)
    for s in signers:
        line(f"{s.sign_order}. {s.display_name} <{s.email}> — {s.party} — "
             f"signed {s.signed_at} — IP {s.signed_ip}")
    y -= 3 * mm
    line("Event trail (hash-chained)", 11, 7 * mm, bold=True)
    for e in events:
        line(f"{e.created_at}  {e.event:<14}  {e.hash[:16]}…", 8, 4.4 * mm)
        if y < 30 * mm:
            c.showPage()
            y = h - 25 * mm
    y -= 3 * mm
    line(f"Chain tip: {chain_tip}", 9, bold=True)
    c.showPage()
    c.save()

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(source)).pages:
        writer.add_page(page)
    for page in PdfReader(io.BytesIO(buf.getvalue())).pages:
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    sealed = out.getvalue()
    sealed_hash = hashlib.sha256(sealed).hexdigest()
    sealed_path = f"esign/sealed/{envelope_id}.pdf"

    await _storage_upload(sealed_path, sealed, "application/pdf")
    conn.execute(text("""
        UPDATE institution.esign_envelope
        SET sealed_path = :p, sealed_sha256 = :h WHERE id = :id
    """), {"id": envelope_id, "p": sealed_path, "h": sealed_hash})
    conn.execute(text(
        "SELECT institution.esign_append_event(:id, NULL, 'sealed', CAST(:d AS jsonb))"
    ), {"id": envelope_id, "d": json.dumps({"sealed_sha256": sealed_hash})})
    conn.commit()
