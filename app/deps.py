# =============================================================================
# ficium-portal-api — Request dependencies
#
# Two authentication paths:
#
#   1. RS256 JWT (portal users — human operators in the browser)
#      Header: Authorization: Bearer <jwt>
#      Resolved via ficium-auth JWKS → current_claims() → tenant_conn()
#
#   2. API Key (machine-to-machine — institution LOS, middleware, scripts)
#      Header: Authorization: Bearer fic_live_<hex>
#      Resolved via institution.api_key (SHA-256 lookup) → api_key_claims()
#
#   api_or_jwt_claims() accepts EITHER, returns a unified claims dict.
#   Use this for any endpoint exposed on the /v1/ public API.
#
# Unified claims shape:
#   JWT path:     standard ficium-auth claims + auth_method="jwt"
#   API key path: {institution_id, scopes, key_id, auth_method="api_key"}
# =============================================================================

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from .core.api_keys import ApiKeyError, verify_api_key
from .core.db import service_session, tenant_session
from .core.security import TokenError, verify_token

_API_KEY_PREFIX = "fic_live_"


async def current_claims(authorization: str = Header(default="")) -> dict[str, Any]:
    """Extract and verify an RS256 JWT bearer token; return its claims or 401."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization[7:].strip()
    try:
        return await verify_token(token)
    except TokenError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


def tenant_conn(
    claims: dict[str, Any] = Depends(current_claims),
) -> Generator[Session]:
    """
    Yield a DB session scoped to the caller via RLS.
    Synchronous generator — FastAPI handles sync dependencies correctly.
    """
    with tenant_session(claims) as session:
        yield session


# ---------------------------------------------------------------------------
# API Key authentication
# ---------------------------------------------------------------------------

async def api_key_claims(
    request: Request,
    authorization: str = Header(default=""),
) -> dict[str, Any]:
    """
    Verify an institution API key (fic_live_*).
    Returns a unified claims dict: {institution_id, scopes, key_id, auth_method}.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    raw_key = authorization[7:].strip()
    if not raw_key.startswith(_API_KEY_PREFIX):
        raise HTTPException(status_code=401, detail="Not a Ficium API key.")

    client_ip = request.client.host if request.client else None

    try:
        with service_session() as conn:
            return verify_api_key(conn, raw_key, client_ip=client_ip)
    except ApiKeyError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


def api_key_conn(
    claims: dict[str, Any] = Depends(api_key_claims),
) -> Generator[Session]:
    """
    Yield a privileged service session for API key authenticated requests.
    Row filtering is done explicitly by institution_id from claims (not RLS).
    """
    with service_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Unified: accepts either JWT (portal) or API key (machine)
# ---------------------------------------------------------------------------

async def api_or_jwt_claims(
    request: Request,
    authorization: str = Header(default=""),
) -> dict[str, Any]:
    """
    Accept EITHER a portal RS256 JWT OR an institution API key.
    Detects by prefix — fic_live_* → API key; anything else → JWT.

    Returns a unified claims dict always containing institution_id.
    For JWT callers, institution_id is taken from the `institution_id` claim.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization[7:].strip()

    if token.startswith(_API_KEY_PREFIX):
        client_ip = request.client.host if request.client else None
        try:
            with service_session() as conn:
                return verify_api_key(conn, token, client_ip=client_ip)
        except ApiKeyError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    # JWT path
    try:
        claims = await verify_token(token)
        claims.setdefault("auth_method", "jwt")
        return claims
    except TokenError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


def require_module(module_key: str):
    """
    Dependency factory — 403s unless the caller's institution is entitled
    to `module_key` in institution.institution.modules (pricing/licensing
    gate, distinct from the per-user group module_permissions RBAC check).

    Usage:
        @router.get("", dependencies=[Depends(require_module("pipeline"))])
    """
    def _check(claims: dict[str, Any] = Depends(current_claims)) -> dict[str, Any]:
        institution_id = claims.get("institution_id")
        if not institution_id:
            raise HTTPException(status_code=403, detail="Not associated with an institution.")
        with service_session() as conn:
            row = conn.execute(
                text("SELECT modules FROM institution.institution WHERE id = :iid"),
                {"iid": institution_id},
            ).fetchone()
        modules = list(row.modules) if row and row.modules else []
        if module_key not in modules:
            raise HTTPException(
                status_code=403,
                detail=f"Your institution is not licensed for the '{module_key}' module.",
            )
        return claims
    return _check


def require_scope(scope: str):
    """
    Dependency factory — injects after api_key_claims and checks a specific scope.
    JWT-authenticated users bypass scope checks (they have full portal access).

    Usage:
        @router.post("/bids", dependencies=[Depends(require_scope("bids:write"))])
    """
    def _check(claims: dict[str, Any] = Depends(api_or_jwt_claims)) -> dict[str, Any]:
        if claims.get("auth_method") == "jwt":
            return claims  # portal users always authorised
        if scope not in claims.get("scopes", []):
            raise HTTPException(
                status_code=403,
                detail=f"API key missing required scope: {scope}",
            )
        return claims
    return _check
