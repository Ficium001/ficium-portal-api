# =============================================================================
# ficium-portal-api — Request dependencies
# Ties verification (security.py) to the RLS-scoped DB session (db.py).
# Endpoints depend on `tenant_conn` and receive a connection already bound to
# the caller's identity, with Postgres RLS enforcing tenant isolation.
# =============================================================================

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import asyncpg
from fastapi import Depends, Header, HTTPException

from .core.db import tenant_session
from .core.security import TokenError, verify_token


async def current_claims(authorization: str = Header(default="")) -> dict[str, Any]:
    """Extract and verify the bearer token; return its claims or 401."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization[7:].strip()
    try:
        return await verify_token(token)
    except TokenError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


async def tenant_conn(
    claims: dict[str, Any] = Depends(current_claims),
) -> AsyncIterator[asyncpg.Connection]:
    """
    Yield a DB connection scoped to the caller via RLS. The same identity
    PostgREST would have injected — only now we control it, on any Postgres.
    """
    async with tenant_session(claims) as conn:
        yield conn
