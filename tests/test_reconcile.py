"""The reconciliation rules, on the drift actually found in production on 2026-10-30."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "reconcile", Path(__file__).parents[1] / "scripts" / "reconcile.py"
)
assert spec and spec.loader
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)


def _app(*reqs, progress=()):
    return {
        "requests": [{"id": i, "status": s, "accepted_bid_id": b} for i, s, b in reqs],
        "deal_progress": [{"request_id": r} for r in progress],
    }


def _portal(reqs, bids=(), pipes=()):
    return {
        "requests": [{"id": i, "status": s} for i, s in reqs],
        "bids": [{"id": i, "request_id": r, "status": s} for i, r, s in bids],
        "pipelines": [{"request_id": r, "status": s} for r, s in pipes],
    }


def rules(found):
    return sorted((r, i) for r, i, _ in found)


def test_in_sync_is_silent() -> None:
    app = _app(("r1", "accepted", "b1"), ("r2", "expired", None), progress=["r1"])
    portal = _portal(
        [("r1", "accepted"), ("r2", "expired")],
        bids=[("b1", "r1", "accepted")],
        pipes=[("r1", "completed")],
    )
    assert reconcile.find(app, portal) == []


def test_the_drift_found_in_production() -> None:
    app = _app(
        ("35572ab8", "accepted", None),  # bank declined, borrower not told
        ("f8a91939", "accepted", None),  # completed at bank, tracker empty
        ("e83914bc", "closed", None),
        ("58a4bc11", "expired", None),
    )
    portal = _portal(
        [
            ("35572ab8", "accepted"),
            ("f8a91939", "accepted"),
            ("e83914bc", "expired"),
            ("58a4bc11", "expired"),
        ],
        bids=[
            ("bA", "35572ab8", "accepted"),
            ("bB", "f8a91939", "accepted"),
            ("bC", "e83914bc", "submitted"),
            ("bD", "58a4bc11", "submitted"),
        ],
        pipes=[("35572ab8", "declined"), ("f8a91939", "completed")],
    )
    assert rules(reconcile.find(app, portal)) == [
        ("R4", "35572ab8"),
        ("R5", "f8a91939"),
        ("R6", "35572ab8"),
        ("R6", "f8a91939"),
        ("R7", "bC"),
        ("R7", "bD"),
    ]


def test_one_sided_and_status_disagreements() -> None:
    app = _app(("only-app", "accepted", None), ("r", "open", None), ("q", "accepted", "x"))
    portal = _portal([("only-portal", "bidding"), ("r", "expired"), ("q", "expired")])
    assert rules(reconcile.find(app, portal)) == [
        ("R1", "only-app"),
        ("R1", "only-portal"),
        ("R2", "q"),
        ("R3", "r"),
    ]
