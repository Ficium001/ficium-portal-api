"""scripts/canary.py: an unsigned probe must read 401 as ON and 503 as OFF."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canary  # noqa: E402


def _with(monkeypatch: pytest.MonkeyPatch, status: int, body: str = "") -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(status, text=body))
    monkeypatch.setattr(canary, "_client", lambda: httpx.Client(transport=transport))


def test_signature_required_means_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _with(monkeypatch, 401, '{"error":"bad_signature"}')
    assert canary.unsigned_probe("https://x/integration/v1/events", "keys not set")()[0] == "PASS"


def test_503_means_off_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    _with(monkeypatch, 503, '{"error":"integration_disabled"}')
    status, detail = canary.unsigned_probe("https://x/integration/v1/events", "keys not set")()
    assert status == "FAIL" and "OFF: keys not set" in detail and "integration_disabled" in detail


@pytest.mark.parametrize("code", [200, 404, 500])
def test_anything_else_fails(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    _with(monkeypatch, code)
    assert canary.unsigned_probe("https://x", "off")()[0] == "FAIL"


def test_health(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"status": "ok", "version": "1.2"})
    )
    monkeypatch.setattr(canary, "_client", lambda: httpx.Client(transport=transport))
    assert canary.health() == ("PASS", "up, version 1.2")


def test_a_crashing_check_is_reported_not_raised() -> None:
    canary.results.clear()

    def boom() -> tuple[str, str]:
        raise RuntimeError("APP_DB_DSN not set")

    canary.record("x", boom)
    assert canary.results[0].status == "FAIL" and "APP_DB_DSN not set" in canary.results[0].detail
    canary.results.clear()
