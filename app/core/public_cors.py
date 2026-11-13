"""
CORS for the two PUBLIC institution-application endpoints, and nothing else.

The portal API's browser origins are the portal front ends only (see config.allowed_origins):
the borrower app is deliberately NOT one, so a bug in it can never become a browser-side
call into the institution API. Institutions, however, apply from the PUBLIC site (the
portal itself is restrictive, with no public registration), so exactly these paths accept
exactly the origins listed in ACCESS_REQUEST_ORIGINS:

    POST /access-requests         POST /access-requests/verify

No credentials are allowed (these endpoints use no cookie or token) and every other
path keeps the normal rules untouched: a request for any other path, or from any other
origin, is passed through to the regular CORS middleware unchanged.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

PUBLIC_PATHS = frozenset({"/access-requests", "/access-requests/verify"})
ALLOW_METHODS = b"POST, OPTIONS"
ALLOW_HEADERS = b"Content-Type"


class PublicApplyCORS:
    def __init__(self, app: ASGIApp, origins: frozenset[str]) -> None:
        self.app = app
        self.origins = origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "").rstrip("/")
        if scope["type"] != "http" or path not in PUBLIC_PATHS or not self.origins:
            await self.app(scope, receive, send)
            return
        origin = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"origin"), None)
        if origin is None or origin not in self.origins:
            await self.app(scope, receive, send)
            return
        allow = origin.encode("latin-1")

        if scope["method"] == "OPTIONS":   # preflight: answered here, never reaches the app
            await send({"type": "http.response.start", "status": 204, "headers": [
                (b"access-control-allow-origin", allow),
                (b"access-control-allow-methods", ALLOW_METHODS),
                (b"access-control-allow-headers", ALLOW_HEADERS),
                (b"access-control-max-age", b"600"),
                (b"vary", b"Origin"), (b"content-length", b"0"),
            ]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                # The regular CORS middleware inside also stamps allow-credentials on every
                # response; these endpoints use no credentials, so drop it.
                drop = (b"access-control-allow-origin", b"access-control-allow-credentials",
                        b"vary")
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in drop]
                headers += [(b"access-control-allow-origin", allow), (b"vary", b"Origin")]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)
