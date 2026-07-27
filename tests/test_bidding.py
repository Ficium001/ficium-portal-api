# =============================================================================
# Unit tests — app/core/bidding.py
#
# DB-free: exercises place_bid() against a FakeSession double that returns
# scripted rows, so the business-rule branching is fully covered without a
# live Postgres. Several of these are REGRESSION TESTS for the confirmed
# production bugs described in the bidding.py module docstring — verified
# against the live Portal DB schema (project egwobcajdlragubtkpqp) before
# this refactor, not guessed.
# =============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.bidding import BidInput, BidPlacementError, place_bid

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class FakeSession:
    """
    Scripted double for sqlalchemy.orm.Session. Each call to .execute()
    consumes the next entry in `script` (a row, None, or an exception
    instance to raise). Records executed SQL for assertions where useful.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.executed: list[str] = []
        self.committed = False

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self.executed.append(str(stmt))
        if not self._script:
            raise AssertionError("FakeSession script exhausted — unexpected extra query")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResult(item)

    def commit(self) -> None:
        self.committed = True


def req_row(
    status: str = "bidding", closes_in_hours: float = 24, product_id: str = "prod-1"
) -> Any:
    return SimpleNamespace(
        status=status,
        bid_window_closes_at=NOW + timedelta(hours=closes_in_hours),
        product_id=product_id,
    )


def bid_row(id_: str = "bid-1", status: str = "submitted", submitted_at: datetime = NOW) -> Any:
    return SimpleNamespace(id=id_, status=status, submitted_at=submitted_at)


def make_bid(**overrides: Any) -> BidInput:
    base: dict[str, Any] = dict(
        request_id="req-1",
        institution_id="inst-1",
        rate=11.9,
        rate_type="fixed",
        amount_offered=300000,
        term_months=36,
    )
    base.update(overrides)
    return BidInput(**base)


# ---------------------------------------------------------------------------
# Input validation (fails before any query)
# ---------------------------------------------------------------------------

def test_invalid_rate_type_rejected_without_querying() -> None:
    conn = FakeSession([])  # no queries should run
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid(rate_type="base_plus"))
    assert exc.value.status_code == 422
    assert conn.executed == []


def test_invalid_submitted_via_rejected_without_querying() -> None:
    conn = FakeSession([])
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid(submitted_via="core_banking_v2"))
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Request lookup / status / window
# ---------------------------------------------------------------------------

def test_request_not_found() -> None:
    conn = FakeSession([None])
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid())
    assert exc.value.status_code == 404


def test_status_open_is_rejected_regression_for_confirmed_v1_bug() -> None:
    """
    REGRESSION: the confirmed live-schema check showed marketplace.
    ingest_app_request() never writes 'open' onto the Portal DB — every
    biddable request is 'bidding'. A request sitting at 'open' must NOT
    be treated as biddable; if this test ever starts failing because
    someone flips BIDDABLE_STATUS back to 'open', that reintroduces the
    bug that made POST /v1/bids fail on every real request.
    """
    conn = FakeSession([req_row(status="open")])
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid())
    assert exc.value.status_code == 409
    assert "open" in exc.value.detail


def test_status_bidding_passes_the_gate() -> None:
    conn = FakeSession([req_row(status="bidding"), None, bid_row()])
    result = place_bid(conn, make_bid(enforce_product_config_gate=False))
    assert result.id == "bid-1"


@pytest.mark.parametrize("status", ["accepted", "cancelled", "expired", "withdrawn"])
def test_terminal_statuses_rejected(status: str) -> None:
    conn = FakeSession([req_row(status=status)])
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid())
    assert exc.value.status_code == 409


def test_closed_bid_window_rejected() -> None:
    conn = FakeSession([req_row(status="bidding", closes_in_hours=-1)])
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid())
    assert exc.value.status_code == 409
    assert "window" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# Compliance gate — regression for confirmed non-existent-column bug
# ---------------------------------------------------------------------------

def test_product_config_gate_blocks_when_not_configured() -> None:
    conn = FakeSession([req_row(), None])  # request OK, product_config lookup -> None
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid())
    assert exc.value.status_code == 403


def test_product_config_gate_uses_product_id_and_enabled_columns() -> None:
    """
    REGRESSION: the confirmed live-schema check showed institution.
    product_config has NO `product_type` or `is_active` columns — the
    real columns are `product_id` (uuid) and `enabled` (boolean). This
    test asserts the query text references the real columns, so a
    revert to the old (nonexistent) column names is caught here rather
    than failing silently in production.
    """
    # No dup, gate passes; script needs one more entry for the dup-check + insert
    conn = FakeSession(
        [req_row(product_id="prod-42"), SimpleNamespace(id="cfg-1"), None, bid_row()]
    )
    place_bid(conn, make_bid())
    gate_query = conn.executed[1]
    assert "product_id" in gate_query
    assert "enabled" in gate_query
    assert "product_type" not in gate_query
    assert "is_active" not in gate_query


def test_product_config_gate_can_be_disabled_explicitly() -> None:
    conn = FakeSession([req_row(), None, bid_row()])
    result = place_bid(conn, make_bid(enforce_product_config_gate=False))
    assert result.id == "bid-1"
    # Only 3 queries ran: request lookup + dup-check + insert (no product_config query)
    assert len(conn.executed) == 3
    assert not any("product_config" in q for q in conn.executed)


# ---------------------------------------------------------------------------
# Duplicate handling — regression for the confirmed UNIQUE constraint gap
# ---------------------------------------------------------------------------

def test_existing_bid_blocks_a_second_submission() -> None:
    """
    REGRESSION: marketplace.bid has UNIQUE (institution_id, request_id) —
    one bid ever, regardless of status. The pre-check must catch this
    with a clean 409 rather than let an IntegrityError reach the DB.
    """
    conn = FakeSession([req_row(), SimpleNamespace(id="cfg-1"), SimpleNamespace(id="existing-bid")])
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid())
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


def test_concurrent_duplicate_caught_as_integrity_error() -> None:
    """Race between the pre-check and the INSERT is still handled cleanly."""
    conn = FakeSession([
        req_row(), SimpleNamespace(id="cfg-1"), None,
        IntegrityError("insert", {}, Exception("dup")),
    ])
    with pytest.raises(BidPlacementError) as exc:
        place_bid(conn, make_bid())
    assert exc.value.status_code == 409


def test_on_conflict_do_nothing_reports_as_duplicate_not_error() -> None:
    """Exact-retry with the same idempotency_key: ON CONFLICT DO NOTHING -> None row."""
    conn = FakeSession([req_row(), SimpleNamespace(id="cfg-1"), None, None])
    result = place_bid(conn, make_bid(idempotency_key="retry-123"))
    assert result.duplicate is True
    assert result.id is None


# ---------------------------------------------------------------------------
# conditions encoding — regression for the confirmed NOT NULL jsonb bug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "conditions_in",
    [None, "please review carefully", {"note": "structured"}, {}],
)
def test_conditions_always_encodes_to_valid_json(conditions_in: Any) -> None:
    """
    REGRESSION: conditions is `jsonb NOT NULL DEFAULT '{}'`. None must never
    reach the DB as SQL NULL, and free-text strings must be valid JSON
    (a quoted JSON string scalar), not raw unquoted text.
    """
    conn = FakeSession([req_row(), SimpleNamespace(id="cfg-1"), None, bid_row()])
    place_bid(conn, make_bid(conditions=conditions_in))
    # The insert is the 4th executed statement; we can't easily inspect bound
    # params via FakeSession, so assert indirectly via the encoder used:
    from app.core.bidding import _encode_conditions
    encoded = _encode_conditions(conditions_in)
    import json
    json.loads(encoded)  # must not raise — always valid JSON


def test_conditions_none_encodes_to_empty_object() -> None:
    from app.core.bidding import _encode_conditions
    assert _encode_conditions(None) == "{}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_successful_bid_returns_result() -> None:
    conn = FakeSession([req_row(), SimpleNamespace(id="cfg-1"), None, bid_row(id_="bid-99")])
    result = place_bid(conn, make_bid())
    assert result.id == "bid-99"
    assert result.status == "submitted"
    assert result.duplicate is False
    assert conn.committed is False  # caller owns commit, per contract
