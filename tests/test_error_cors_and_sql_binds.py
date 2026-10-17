"""
Two regressions found together in Oct 2026 via the workflow editor's
"Failed to fetch":

1. A 500 carried no CORS header, so browsers reported a CORS error instead
   of the failure (the Aug 2026 handler assumed it ran inside CORSMiddleware;
   Starlette actually runs Exception handlers outermost).
2. `:param::type` in text() SQL is not a bind parameter in SQLAlchemy (the
   `::` stops the match), so Postgres received a literal ":sk::..." and every
   workflow create / add-stage / edit-stage failed with a syntax error.
"""
from __future__ import annotations

import os
import pathlib
import re

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app import main  # noqa: E402

_boom_path = "/__test_boom"
if not any(getattr(r, "path", None) == _boom_path for r in main.app.routes):
    @main.app.get(_boom_path, include_in_schema=False)
    def _boom() -> None:
        raise RuntimeError("deliberate")


def _get(origin: str | None):
    headers = {"Origin": origin} if origin else {}
    return TestClient(main.app, raise_server_exceptions=False).get(_boom_path, headers=headers)


def test_500_from_allowed_origin_is_readable_by_the_browser() -> None:
    allowed = main._origins[0] if main._origins else None
    if allowed is None:  # regex-only config
        return
    r = _get(allowed)
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == allowed
    assert r.json() == {"detail": "Internal server error."}


def test_500_to_unknown_origin_gets_no_cors() -> None:
    r = _get("https://evil.example")
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


def test_no_param_cast_shorthand_in_sql() -> None:
    """`:name::type` silently isn't bound. Use CAST(:name AS type)."""
    bad = re.compile(r"(?<![:\w]):[A-Za-z_]\w*::")
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    hits = [f"{p.relative_to(root)}:{i}"
            for p in root.rglob("*.py")
            for i, line in enumerate(p.read_text().splitlines(), 1) if bad.search(line)]
    assert hits == [], hits
