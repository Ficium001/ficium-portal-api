# =============================================================================
# ficium-portal-api — Ficium App (borrower) token verification
#
# Separate trust path from core/security.py's verify_token(), which only
# trusts ficium-auth RS256 tokens (institution portal staff). Borrower
# sessions are issued by Supabase Auth on the Ficium App project — a
# different signer, issuer, and audience entirely, so they cannot be
# verified against the ficium-auth JWKS.
#
# Rather than adding a second JWKS client (Supabase's asymmetric-key JWKS
# endpoint shape/rotation isn't guaranteed to match ficium-auth's), this
# verifies via GoTrue's own introspection endpoint: GET /auth/v1/user with
# the borrower's access token as Bearer. Supabase validates the token
# server-side and returns the user, or 401s. One extra network round trip
# per call, on a low-traffic borrower-facing read endpoint — an acceptable
# trade for not hand-rolling verification against another project's keys.
#
# Only the anon/publishable key is used (required by GoTrue as the apikey
# header on every request, borrower's own token or not) — never the App
# project's service_role key, which must never leave the App project.
# =============================================================================

from __future__ import annotations

from typing import Any

import httpx
import structlog

from .config import settings

log = structlog.get_logger()


class AppAuthError(Exception):
    """Raised when a Ficium App borrower token is missing, malformed, or untrusted."""


async def verify_app_user(token: str) -> dict[str, Any]:
    """
    Verify a Ficium App (Supabase) borrower access token and return the
    GoTrue user record (id, email, ...). Raises AppAuthError on any failure
    (caller maps to 401) — including when the App Supabase project isn't
    configured, so this fails closed rather than silently trusting nothing.
    """
    if not settings.app_supabase_url or not settings.app_supabase_anon_key:
        raise AppAuthError("Borrower auth is not configured on this deployment.")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{settings.app_supabase_url.rstrip('/')}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": settings.app_supabase_anon_key,
                },
            )
    except httpx.HTTPError as e:
        log.warning("app_auth_upstream_error", error=str(e))
        raise AppAuthError("Could not reach borrower auth service.") from e

    if resp.status_code != 200:
        raise AppAuthError("Invalid or expired session.")

    user = resp.json()
    if not user.get("id"):
        raise AppAuthError("Malformed user record from auth service.")
    return user
