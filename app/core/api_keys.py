# =============================================================================
# ficium-portal-api — API Key verification
#
# Key format:  fic_live_<64 hex chars>   (32 random bytes, hex-encoded)
# Storage:     key_prefix  = first 12 visible chars (e.g. fic_live_a3b4)
#              key_hash    = SHA-256 of the full raw key (hex digest)
# Lookup:      hash incoming bearer → SELECT WHERE key_hash = ?
#
# Security properties:
#  - Raw key is NEVER stored; only its SHA-256 hash
#  - key_prefix surfaces in the portal UI so admins can identify keys
#  - Keys require maker-checker approval (mc_status = 'approved')
#  - Keys can be revoked (revoked_at IS NOT NULL → rejected)
#  - Keys can expire (expires_at < now() → rejected)
#  - last_used_at and last_used_ip are updated on each successful auth
#    (best-effort; failure to update does NOT reject the request)
# =============================================================================

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

log = structlog.get_logger()

# Public API key prefix
_PREFIX = "fic_live_"
_KEY_BYTES = 32  # 256-bit key → 64 hex chars


class ApiKeyError(Exception):
    """Raised when an API key is missing, invalid, revoked, or expired."""


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        (raw_key, key_prefix, key_hash)
        raw_key    — the value shown to the user ONCE (never stored)
        key_prefix — first 12 visible chars, stored for UI display
        key_hash   — SHA-256(raw_key), stored for lookup
    """
    raw = _PREFIX + secrets.token_hex(_KEY_BYTES)
    key_prefix = raw[:12]
    key_hash = _hash_key(raw)
    return raw, key_prefix, key_hash


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_api_key(conn: Session, raw_key: str, client_ip: str | None = None) -> dict[str, Any]:
    """
    Verify a raw API key against the database.

    Returns a claims-like dict:
        {
            "institution_id": str,
            "scopes": list[str],
            "key_id": str,
            "auth_method": "api_key",
        }

    Raises ApiKeyError on any failure.
    Updates last_used_at / last_used_ip best-effort (does not raise on failure).
    """
    if not raw_key.startswith(_PREFIX):
        raise ApiKeyError("Not a Ficium API key.")

    key_hash = _hash_key(raw_key)

    row = conn.execute(
        text("""
            SELECT
                id,
                institution_id,
                scopes,
                active,
                mc_status,
                expires_at,
                revoked_at
            FROM institution.api_key
            WHERE key_hash = :kh
        """),
        {"kh": key_hash},
    ).fetchone()

    if row is None:
        raise ApiKeyError("Invalid API key.")

    if not row.active:
        raise ApiKeyError("API key is inactive.")

    if row.mc_status != "approved":
        raise ApiKeyError(f"API key not yet approved (status: {row.mc_status}).")

    if row.revoked_at is not None:
        raise ApiKeyError("API key has been revoked.")

    # Import here to avoid circular; sqlalchemy text + now() comparison
    from datetime import datetime, timezone
    if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
        raise ApiKeyError("API key has expired.")

    # Best-effort telemetry update
    try:
        conn.execute(
            text("""
                UPDATE institution.api_key
                SET last_used_at = now(),
                    last_used_ip = CAST(:ip AS inet)
                WHERE id = :kid
            """),
            {"kid": str(row.id), "ip": client_ip or "0.0.0.0"},
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("api_key_telemetry_failed", key_id=str(row.id))
        conn.rollback()

    log.info("api_key_auth_ok", institution_id=str(row.institution_id), key_id=str(row.id))

    return {
        "institution_id": str(row.institution_id),
        "scopes": list(row.scopes or []),
        "key_id": str(row.id),
        "auth_method": "api_key",
    }
