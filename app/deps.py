# =============================================================================
# ficium-portal-api — Request dependencies (psycopg2 / SQLAlchemy version)
# =============================================================================

from __future__ import annotations

from typing import Any, Generator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

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


def tenant_conn(
    claims: dict[str, Any] = Depends(current_claims),
) -> Generator[Session, None, None]:
    """
    Yield a DB session scoped to the caller via RLS.
    Synchronous generator — FastAPI handles sync dependencies correctly.
    """
    with tenant_session(claims) as session:
        yield session
