"""The public site may call exactly the two public application endpoints, nothing else.

The portal API's browser origins are the portal front ends only: the borrower app is
deliberately not an allowed browser origin (see config.allowed_origins), so this exception is
scoped to two paths and an explicit, environment-supplied origin list. The origin below is a
test value; the service itself never hard-codes the borrower app's address.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.core.public_cors import PublicApplyCORS

SITE = "https://site.example"
EVIL = "https://evil.example"
PORTAL = "https://portal.example"


def build(origins: frozenset[str]) -> TestClient:
    """The same stack as main.py: regular CORS inside, the scoped exception outermost."""
    api = FastAPI()

    @api.post("/access-requests")
    def apply() -> dict:
        return {"ok": True}

    @api.post("/access-requests/verify")
    def verify() -> dict:
        return {"ok": True}

    @api.post("/auth/login")
    def login() -> dict:
        return {"ok": True}

    api.add_middleware(
        CORSMiddleware, allow_origins=[PORTAL], allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"],
    )
    api.add_middleware(PublicApplyCORS, origins=origins)
    return TestClient(api, raise_server_exceptions=False)


def preflight(c: TestClient, path: str, origin: str):
    return c.options(path, headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                    "Access-Control-Request-Headers": "content-type"})


@pytest.mark.parametrize("path", ["/access-requests", "/access-requests/verify"])
def test_the_public_site_may_preflight_the_two_public_endpoints(path: str) -> None:
    r = preflight(build(frozenset({SITE})), path, SITE)
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == SITE
    assert r.headers["access-control-allow-methods"] == "POST, OPTIONS"
    assert r.headers["access-control-allow-headers"] == "Content-Type"
    assert "access-control-allow-credentials" not in r.headers
    assert r.headers["vary"] == "Origin"


def test_a_post_from_the_public_site_is_allowed_without_credentials() -> None:
    r = build(frozenset({SITE})).post("/access-requests", headers={"Origin": SITE})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == SITE
    assert "access-control-allow-credentials" not in r.headers  # the inner one's is stripped


@pytest.mark.parametrize("origin", [EVIL, SITE + ".evil.example", "http://site.example"])
def test_any_other_origin_is_not_granted_the_exception(origin: str) -> None:
    c = build(frozenset({SITE}))
    assert "access-control-allow-origin" not in preflight(c, "/access-requests", origin).headers
    assert "access-control-allow-origin" not in c.post(
        "/access-requests", headers={"Origin": origin}).headers


def test_the_portal_origin_is_unaffected_by_the_exception() -> None:
    c = build(frozenset({SITE}))
    r = c.post("/access-requests", headers={"Origin": PORTAL})
    assert r.headers["access-control-allow-origin"] == PORTAL      # its normal, global right


@pytest.mark.parametrize("path", ["/auth/login", "/access-requests/other", "/health"])
def test_the_public_site_gets_no_other_path(path: str) -> None:
    c = build(frozenset({SITE}))
    assert "access-control-allow-origin" not in preflight(c, path, SITE).headers
    assert "access-control-allow-origin" not in c.post(path, headers={"Origin": SITE}).headers


def test_the_portal_keeps_its_normal_rules() -> None:
    c = build(frozenset({SITE}))
    r = preflight(c, "/auth/login", PORTAL)
    assert r.headers["access-control-allow-origin"] == PORTAL
    assert r.headers["access-control-allow-credentials"] == "true"


def test_no_origins_configured_means_nobody() -> None:
    c = build(frozenset())
    assert "access-control-allow-origin" not in preflight(c, "/access-requests", SITE).headers
    assert "access-control-allow-origin" not in c.post(
        "/access-requests", headers={"Origin": SITE}).headers


def test_the_real_app_is_closed_by_default() -> None:
    from app.main import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/access-requests", json={}, headers={"Origin": "https://anything.example"})
    assert "access-control-allow-origin" not in r.headers
