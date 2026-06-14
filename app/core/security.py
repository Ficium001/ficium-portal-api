# =============================================================================
# ficium-portal-api — Token verification
# Verifies ficium-auth RS256 access tokens against the published JWKS.
# No shared secret. Caches the keyset; refreshes on unknown kid (rotation).
# =============================================================================

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from jose import jwt
from jose.exceptions import JOSEError

from .config import settings

log = structlog.get_logger()


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or untrusted."""


class _JWKSCache:
    """Fetch-and-cache the ficium-auth JWKS; force-refresh on unknown kid."""

    def __init__(self, url: str, ttl: int) -> None:
        self._url = url
        self._ttl = ttl
        self._keys: dict[str, dict] = {}
        self._fetched_at: float = 0.0

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(self._url)
            resp.raise_for_status()
            data = resp.json()
        self._keys = {k["kid"]: k for k in data.get("keys", []) if "kid" in k}
        self._fetched_at = time.monotonic()
        log.info("jwks_refreshed", count=len(self._keys))

    async def get_key(self, kid: str) -> dict:
        stale = (time.monotonic() - self._fetched_at) > self._ttl
        if kid not in self._keys or stale:
            await self._refresh()
        if kid not in self._keys:
            # Unknown kid even after refresh → genuinely untrusted.
            raise TokenError("Token signed with an unknown key.")
        return self._keys[kid]


_jwks = _JWKSCache(settings.auth_jwks_url, settings.jwks_cache_ttl)


async def verify_token(token: str) -> dict[str, Any]:
    """
    Verify a ficium-auth access token and return its claims.
    Raises TokenError on any failure (caller maps to 401).
    """
    try:
        header = jwt.get_unverified_header(token)
    except JOSEError as e:
        raise TokenError("Malformed token.") from e

    kid = header.get("kid")
    if not kid:
        raise TokenError("Token missing key id.")

    key = await _jwks.get_key(kid)

    try:
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.auth_audience,
            issuer=settings.auth_issuer,
        )
    except JOSEError as e:
        # Expired, bad signature, wrong issuer/audience — all untrusted.
        raise TokenError(str(e)) from e
