"""Vendored wheels match vendor/SHA256SUMS, and requirements.txt points at them."""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"


def _sums() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (VENDOR / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        out[name.lstrip("*")] = digest
    return out


def test_every_vendored_wheel_matches_its_checksum() -> None:
    sums = _sums()
    wheels = sorted(p.name for p in VENDOR.glob("*.whl"))
    assert wheels, "no wheels in vendor/"
    assert sorted(sums) == wheels, "SHA256SUMS must list exactly the wheels in vendor/"
    for name in wheels:
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == sums[name], name


def test_requirements_install_the_vendored_wheels() -> None:
    lines = [ln.strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()]
    for name in _sums():
        assert f"./vendor/{name}" in lines, f"requirements.txt does not install {name}"
    assert not any("github.com" in ln for ln in lines if not ln.startswith("#")), (
        "no requirement may be fetched from GitHub (private repos 404 without a token)"
    )
