# =============================================================================
# ficium-portal-api — Canonical bid-placement service (mkt:bidding)
#
# ONE code path for creating a marketplace.bid row — shared by
# app/api/marketplace.py (portal UI), app/api/v1/marketplace.py
# (institution API), and the future autobid worker.
#
# This module replaces two independent, divergent implementations. Rather
# than guess which one was "correct" and merge behavior blindly, both were
# checked directly against the LIVE Portal DB schema (project
# egwobcajdlragubtkpqp, read-only queries) before anything was written here.
# That check surfaced concrete, CONFIRMED bugs — recorded below so the
# history isn't lost and so nobody re-introduces them.
#
# -----------------------------------------------------------------------
# CONFIRMED FINDINGS (verified against live schema + data, 2026-07-06):
# -----------------------------------------------------------------------
#
# 1. POST /v1/bids has never produced a successful bid. Confirmed empirically:
#    zero rows in marketplace.bid have submitted_via='api', ever — only
#    'portal'. Root causes, all now fixed here:
#
#      a. Its first query selected a `product_type` column from
#         marketplace.request that DOES NOT EXIST (the column is
#         `product_id`, a uuid FK to catalog.product). This query would
#         raise "column does not exist" before any business logic ran.
#
#      b. Its compliance-gate query referenced `institution.product_config
#         .product_type` and `.is_active` — NEITHER column exists. The real
#         columns are `product_id` (uuid) and `enabled` (boolean), and
#         product_config keys directly on product_id — no code lookup
#         needed.
#
#      c. Its status gate checked `request.status == 'open'`. But
#         marketplace.ingest_app_request() (the App DB -> Portal DB sync
#         function) never writes 'open' — every biddable request lands as
#         'bidding'. 'open' is a legal value per the CHECK constraint but is
#         not a value ingested requests ever take on this table.
#
#    Even had (a) and (b) not existed, (c) alone would have rejected every
#    real request. All three compound, which is why this endpoint could
#    never have completed a bid.
#
# 2. Neither endpoint's duplicate-bid handling matched the DB's actual
#    constraint. marketplace.bid has a hard
#    `UNIQUE (institution_id, request_id)` — one bid ever, full stop,
#    regardless of status or idempotency_key. The legacy endpoint's
#    `ON CONFLICT (institution_id, request_id, idempotency_key)` only
#    targets the NARROWER 3-column unique index; a second attempt with a
#    different idempotency_key does not hit that target and instead raises
#    an unhandled UniqueViolation on the 2-column constraint, downgraded by
#    the legacy endpoint's generic `except Exception` to a 400 with a raw
#    Postgres error string. The (never-reached) v1 "exclude withdrawn/
#    rejected" pre-check was moot for the same reason: the DB does not allow
#    a fresh bid after withdrawal/rejection either. This service pre-checks
#    for ANY existing bid on (institution_id, request_id) and returns a
#    clean 409; the ON CONFLICT is retained purely as a race-safety net
#    between the pre-check and the INSERT (classic TOCTOU), same discipline
#    as the atomic PII-reveal transaction elsewhere in this codebase.
#
# 3. `conditions` is `jsonb NOT NULL DEFAULT '{}'`. The legacy path already
#    handled this correctly. The v1 path bound its free-text `str | None`
#    field directly with no cast: None -> NOT NULL violation; any non-JSON
#    string -> "invalid input syntax for type json". Fixed by JSON-encoding
#    whatever shape the caller provides (dict or plain string).
#
# 4. Minor: v1's Pydantic model accepted rate_type
#    fixed/variable/base_plus, but bid_rate_type_check only allows
#    fixed/variable. Tightened in app/api/v1/marketplace.py to match the DB.
#
# -----------------------------------------------------------------------
# NEW ENFORCEMENT (flagged, not silent): the product_config compliance gate
# is now applied on BOTH paths. It was previously broken on v1 (see 1b) and
# entirely absent on the portal path. Verified against live data before
# enabling: the one institution with existing bids has enabled=true
# product_config rows for both products it has bid on, so this does not
# retroactively break anything observed in the current dataset — but it IS
# a genuinely new gate for the portal endpoint and should be reviewed.
#
# `submitted_via` CHECK currently allows only
# portal / api / webhook / core_banking — 'autobid' is NOT yet a valid
# value. The worker PR must either add it via migration or tag
# autobid-originated bids as 'api' (recommended interim: attribution
# already lives in autobid.execution via rule_version_id, so 'api' is a
# safe placeholder until a dedicated value is added).
# =============================================================================

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# Confirmed against marketplace.ingest_app_request(): every request synced
# from the App DB lands in this status once its bid window is active.
BIDDABLE_STATUS = "bidding"

# Confirmed against bid_rate_type_check.
VALID_RATE_TYPES = frozenset({"fixed", "variable"})

# Confirmed against bid_submitted_via_check.
VALID_SUBMITTED_VIA = frozenset({"portal", "api", "webhook", "core_banking"})


@dataclass(frozen=True)
class BidInput:
    request_id: str
    institution_id: str
    rate: float
    rate_type: str
    amount_offered: float
    term_months: int
    rate_valid_days: int | None = None
    conditions: dict[str, Any] | str | None = None
    fee_structure: dict[str, Any] | None = None
    idempotency_key: str | None = None
    submitted_via: str = "portal"
    enforce_product_config_gate: bool = True


@dataclass(frozen=True)
class BidResult:
    id: str | None
    status: str
    submitted_at: datetime | None
    duplicate: bool = False


class BidPlacementError(Exception):
    """Business-rule rejection. Carries an HTTP status code + detail message."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _encode_conditions(value: dict[str, Any] | str | None) -> str:
    """conditions is jsonb NOT NULL DEFAULT '{}' — always emit valid JSON."""
    if value is None:
        return json.dumps({})
    return json.dumps(value)


def place_bid(conn: Session, bid: BidInput) -> BidResult:
    """
    Insert one marketplace.bid row under a single, verified set of business
    rules. Raises BidPlacementError for any rejection. Caller owns the
    transaction: call conn.commit() after this returns successfully.
    """
    if bid.rate_type not in VALID_RATE_TYPES:
        raise BidPlacementError(
            422, f"Unsupported rate_type: {bid.rate_type!r} (must be fixed or variable)."
        )
    if bid.submitted_via not in VALID_SUBMITTED_VIA:
        raise BidPlacementError(422, f"Unsupported submitted_via: {bid.submitted_via!r}.")

    req = conn.execute(
        text("""
            SELECT status, bid_window_closes_at, product_id
            FROM marketplace.request
            WHERE id = CAST(:rid AS uuid)
        """),
        {"rid": bid.request_id},
    ).fetchone()
    if req is None:
        raise BidPlacementError(404, "Request not found.")
    if req.status != BIDDABLE_STATUS:
        raise BidPlacementError(
            409, f"Request is not open for bidding (status: {req.status})."
        )
    if req.bid_window_closes_at and req.bid_window_closes_at < datetime.now(UTC):
        raise BidPlacementError(409, "Bid window has closed for this request.")

    if bid.enforce_product_config_gate:
        config = conn.execute(
            text("""
                SELECT id FROM institution.product_config
                WHERE institution_id = CAST(:iid AS uuid)
                  AND product_id     = CAST(:pid AS uuid)
                  AND enabled        = true
            """),
            {"iid": bid.institution_id, "pid": str(req.product_id)},
        ).fetchone()
        if config is None:
            raise BidPlacementError(
                403, "Institution is not configured to bid on this product."
            )

    # DB enforces UNIQUE (institution_id, request_id) — pre-check for a clean
    # error message; the ON CONFLICT below is a race-safety net only.
    dup = conn.execute(
        text("""
            SELECT id FROM marketplace.bid
            WHERE institution_id = CAST(:iid AS uuid)
              AND request_id     = CAST(:rid AS uuid)
        """),
        {"iid": bid.institution_id, "rid": bid.request_id},
    ).fetchone()
    if dup is not None:
        raise BidPlacementError(409, "A bid already exists for this request.")

    try:
        row = conn.execute(
            text("""
                INSERT INTO marketplace.bid
                    (request_id, institution_id, rate, rate_type, rate_valid_days,
                     amount_offered, term_months, conditions, fee_structure,
                     submitted_via, idempotency_key)
                VALUES
                    (CAST(:request_id AS uuid), CAST(:institution_id AS uuid), :rate,
                     :rate_type, :rate_valid_days, :amount_offered, :term_months,
                     CAST(:conditions AS jsonb), CAST(:fee_structure AS jsonb),
                     :submitted_via, :idempotency_key)
                ON CONFLICT (institution_id, request_id, idempotency_key) DO NOTHING
                RETURNING id, status, submitted_at
            """),
            {
                "request_id": bid.request_id,
                "institution_id": bid.institution_id,
                "rate": bid.rate,
                "rate_type": bid.rate_type,
                "rate_valid_days": bid.rate_valid_days,
                "amount_offered": bid.amount_offered,
                "term_months": bid.term_months,
                "conditions": _encode_conditions(bid.conditions),
                "fee_structure": json.dumps(bid.fee_structure or {}),
                "submitted_via": bid.submitted_via,
                "idempotency_key": bid.idempotency_key,
            },
        ).fetchone()
    except IntegrityError as e:
        # Race: two concurrent callers both passed the pre-check above.
        raise BidPlacementError(409, "A bid already exists for this request.") from e

    if row is None:
        # ON CONFLICT DO NOTHING fired: an exact-retry with the same
        # idempotency_key. Not an error — report it as such to the caller.
        return BidResult(id=None, status="duplicate", submitted_at=None, duplicate=True)
    return BidResult(id=str(row.id), status=row.status, submitted_at=row.submitted_at)
