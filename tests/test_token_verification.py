"""verify_token with real RS256 crypto: ficium-auth's tokens pass, everything else fails."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.core import security
from app.core.config import settings

KID = "test-kid"
_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_pub = _priv.public_key()
JWK = {**RSAAlgorithm.to_jwk(_pub, as_dict=True), "kid": KID, "alg": "RS256", "use": "sig"}
PEM_PUB = _pub.public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


def _token(**over: object) -> str:
    now = int(time.time())
    claims = {"sub": "user-1", "aud": settings.auth_audience, "iss": settings.auth_issuer,
              "iat": now, "exp": now + 300}
    claims.update(over)
    return jwt.encode(claims, _priv, algorithm="RS256", headers={"kid": KID})


@pytest.fixture(autouse=True)
def _jwks():
    with patch.object(security._jwks, "get_key", AsyncMock(return_value=JWK)):
        yield


async def test_genuine_token_is_accepted() -> None:
    assert (await security.verify_token(_token()))["sub"] == "user-1"


@pytest.mark.parametrize("over", [
    {"exp": int(time.time()) - 10},
    {"aud": "someone-else"},
    {"iss": "not-ficium-auth"},
])
async def test_expired_or_misaddressed_token_is_rejected(over: dict) -> None:
    with pytest.raises(security.TokenError):
        await security.verify_token(_token(**over))


async def test_hs256_forged_with_the_public_key_is_rejected() -> None:
    """The python-jose flaw (PYSEC-2026-4298): sign HS256 using the public key as the secret."""
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    head = b64(json.dumps({"alg": "HS256", "kid": KID, "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": "attacker", "aud": settings.auth_audience,
                           "iss": settings.auth_issuer, "exp": int(time.time()) + 300}).encode())
    sig = b64(hmac.new(PEM_PUB, f"{head}.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(security.TokenError):
        await security.verify_token(f"{head}.{body}.{sig}")


async def test_tampered_signature_is_rejected() -> None:
    head, body, sig = _token().split(".")
    with pytest.raises(security.TokenError):
        await security.verify_token(f"{head}.{body}.{sig[:-4]}AAAA")


async def test_malformed_token_is_rejected_before_any_key_lookup() -> None:
    with pytest.raises(security.TokenError):
        await security.verify_token("not-a-jwt")
