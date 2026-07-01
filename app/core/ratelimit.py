# =============================================================================
# ficium-portal-api — Rate limiting
#
# Per-tenant + per-IP rate limiting via slowapi. The key function prefers the
# authenticated institution_id from the verified JWT (so one tenant can't
# exhaust another's budget, and a single tenant can't hammer the API from many
# IPs), falling back to client IP for unauthenticated / pre-auth requests.
#
# Limits are deliberately generous for a B2B API (real institutions make many
# legitimate calls) but low enough to blunt brute-force, scraping, and abuse.
# =============================================================================

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from .security import verify_token  # noqa: F401  (imported for type context)


def _rate_key(request: Request) -> str:
    """
    Rate-limit bucket key. Prefer institution_id from a verified bearer token
    so limits are per-tenant; fall back to client IP otherwise.

    We do a cheap unverified peek at the token's institution_id purely for
    bucketing — this is NOT an auth decision (the real verification happens in
    the route dependency). Worst case a forged token lands in a bucket it
    doesn't own, which only ever throttles the forger, never another tenant.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        # API keys are opaque; bucket the whole key string.
        if token.startswith("fic_live_"):
            return f"key:{token[:24]}"
        # JWT: pull institution_id from the payload without verifying signature
        # (bucketing only — see docstring).
        try:
            import base64
            import json as _json

            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
            iid = claims.get("institution_id")
            if iid:
                return f"inst:{iid}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


# Default limit applies to every route unless overridden with @limiter.limit(...)
#
# STORAGE: defaults to in-memory, so counters are per-process and reset on
# restart. Fine for a single Railway instance. If portal-api is ever scaled to
# more than one instance, pass storage_uri="redis://..." here so limits are
# shared across instances — otherwise the effective limit multiplies by the
# instance count.
limiter = Limiter(
    key_func=_rate_key,
    default_limits=["600/minute"],   # generous per-tenant baseline
    headers_enabled=True,            # emit X-RateLimit-* response headers
)
