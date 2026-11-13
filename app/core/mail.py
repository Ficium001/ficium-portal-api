"""Transactional email through Resend, same rules as e-sign (app/api/esign.py).

In production a missing RESEND_API_KEY or a refused send raises EmailNotSent: the
caller decides what that means. Outside production the message is only logged, so
development works without a mail provider. Links in a message are never logged in
production.
"""

from __future__ import annotations

import os

import httpx
import structlog

from .config import settings

log = structlog.get_logger()


class EmailNotSent(Exception):
    pass


async def send_email(to: str, subject: str, html: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        if settings.env == "production":
            log.error("email_not_configured", to_domain=to.rsplit("@", 1)[-1])
            raise EmailNotSent("email is not configured")
        log.warning("email_dev_mode", to=to, subject=subject, body=html)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": os.environ.get("PORTAL_EMAIL_FROM", "Ficium <no-reply@ficium.net>"),
                    "to": [to], "subject": subject, "html": html,
                },
            )
    except httpx.HTTPError as e:
        log.error("email_unreachable", error=type(e).__name__)
        raise EmailNotSent("email provider unreachable") from e
    if resp.status_code not in (200, 201):
        log.error("email_failed", status=resp.status_code, detail=resp.text[:300])
        raise EmailNotSent(f"email provider refused ({resp.status_code})")
