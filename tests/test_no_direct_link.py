"""The borrower app and the portal talk only through the integration contract (Oct 2026).

A repo-wide guard so no direct link creeps back: no call to the borrower app's address, no shared
service secret, no session to its database, no check of its logins.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from unittest.mock import patch

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
FORBIDDEN = [
    (re.compile(r"ficium\.vercel\.app|vercel_app_url"), "the borrower app's address"),
    (re.compile(r"X-Service-Secret|app_service_secret"), "the shared service secret"),
    (
        re.compile(r"app_service_session|app_database_url|_app_engine"),
        "a borrower-database session",
    ),
    (
        re.compile(r"app_supabase_url|verify_app_user|/auth/v1/user"),
        "a check of borrower-app logins",
    ),
]


@pytest.mark.parametrize(("pattern", "what"), FORBIDDEN, ids=[w for _, w in FORBIDDEN])
def test_no_direct_link_in_code(pattern, what):
    hits = []
    for f in APP.rglob("*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]  # comments may explain the history
            if pattern.search(code):
                hits.append(f"{f.relative_to(APP)}:{n}")
    assert hits == [], f"{what}: {hits}"


def test_market_intelligence_is_published_at_most_hourly():
    from app import maintenance as m

    calls = []
    with patch.object(m, "_publish_market", lambda: calls.append(1) or True):
        m._last_market = 0.0
        assert asyncio.run(m.publish_market_if_due(now=10_000.0)) is True
        assert asyncio.run(m.publish_market_if_due(now=10_000.0 + 60)) is None  # throttled
        assert asyncio.run(m.publish_market_if_due(now=10_000.0 + m.MARKET_EVERY_S)) is True
    assert len(calls) == 2
