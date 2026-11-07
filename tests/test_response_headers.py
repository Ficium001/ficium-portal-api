"""Every response carries the transport/security defaults (HSTS, nosniff, no-store)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.response_headers import DefaultResponseHeadersMiddleware


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(DefaultResponseHeadersMiddleware)

    @app.get("/x")
    def x() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_security_defaults_on_every_response() -> None:
    h = _client().get("/x").headers
    assert h["strict-transport-security"] == "max-age=63072000; includeSubDomains"
    assert h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-store"
