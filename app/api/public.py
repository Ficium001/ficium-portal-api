# =============================================================================
# ficium-portal-api — Public router (server-to-server, no JWT) v2 schema
# =============================================================================

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import text

from ..core.config import settings
from ..core.db import service_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])


def _verify_secret(received: str) -> None:
    expected = settings.app_service_secret
    if not expected:
        raise HTTPException(status_code=503, detail="Service-to-service auth not configured.")
    if not hmac.compare_digest(received.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid service secret.")




# (removed) GET /public/requests/{id}/bids and POST /public/requests/bids/bulk: they were
# direct links from the borrower app (shared secret + the borrower's account id). The app now
# reads its own copy of the bid.* contract events (integration.bid_shadow, ficium #89).


# (removed) POST /public/requests/{id}/accept-bid: the legacy acceptance path. It took the
# borrower's raw account id and md5-hashed it into the portal's borrower id (unkeyed, so anyone
# knowing an App id could compute it). Acceptance is now only the contract call
# (POST /integration/v1/acceptances, acceptance.py), and borrower ids are the contract's keyed
# anon_borrower_id (db/034). after_acceptance moved to app/core/acceptance_effects.py.


# ── (removed) GET /public/requests/{request_id}/pipeline ──────────────────────
# It was removed (Oct 2026): it was a direct link from the
# borrower app (shared secret + the borrower's account id). The loan tracker now runs on
# deal.progress_updated events (contract 1.7.0, db/033).


# ── GET /public/market-intelligence ──────────────────────────────────────────
# Aggregates that used to live as App DB views (v_market_rates,
# v_acceptance_intelligence, v_market_competitiveness) before bids moved to
# the Portal DB's marketplace.bid table. Those views were never repointed —
# they still queried a legacy institution.institution_bids table that no
# longer exists — so this is the replacement: compute the same three
# aggregates here, against the data that's actually live, and let
# api/intelligence.ts (App DB / Vercel side) call this instead.
#
# catalog.product.code is the Portal DB's canonical product vocabulary and
# doesn't map 1:1 onto the App DB's ProductType strings (e.g. 'mortgage' on
# the App side is 'home_loan' here, and a few App types collapse onto one
# catalog code — see catalog.product_id_for_app_type()). _APP_PRODUCT_TYPE
# is the practical inverse for display purposes; it's lossy for the
# few-to-one cases, which is acceptable since this feeds human-readable
# insight text, not exact-match business logic.
_APP_PRODUCT_TYPE: dict[str, str] = {
    "home_loan":       "mortgage",
    "personal_loan":   "personal_loan",
    "business_loan":   "business_loan",
    "deposit":         "fixed_deposit",
    "savings":         "savings_account",
    "credit_card":     "credit_card",
    "education_loan":  "education_loan",
    "vehicle_loan":    "leasing",
}


def _app_type(code: str) -> str:
    return _APP_PRODUCT_TYPE.get(code, code)


@router.get("/market-intelligence")
async def market_intelligence(
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> dict:
    _verify_secret(x_service_secret)

    with service_session() as conn:
        rate_rows = conn.execute(text("""
            SELECT
                p.code                                                      AS product_code,
                COUNT(DISTINCT b.id)                                        AS bid_count,
                COUNT(DISTINCT r.id)                                        AS request_count,
                ROUND((MIN(b.rate) * 100)::numeric, 2)                      AS min_rate_pct,
                ROUND((MAX(b.rate) * 100)::numeric, 2)                      AS max_rate_pct,
                ROUND((AVG(b.rate) * 100)::numeric, 2)                      AS avg_rate_pct,
                ROUND((PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY b.rate) * 100)::numeric, 2) AS p25_rate_pct,
                ROUND((PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY b.rate) * 100)::numeric, 2) AS p75_rate_pct
            FROM marketplace.bid b
            JOIN marketplace.request r ON r.id = b.request_id
            JOIN catalog.product    p ON p.id = r.product_id
            WHERE b.status IN ('submitted', 'accepted')
              AND b.created_at > now() - interval '90 days'
            GROUP BY p.code
        """)).fetchall()

        acceptance_rows = conn.execute(text("""
            SELECT
                p.code                                            AS product_code,
                COUNT(*)                                          AS total_acceptances,
                ROUND((AVG(b.rate) * 100)::numeric, 2)            AS avg_winning_rate_pct,
                ROUND((MIN(b.rate) * 100)::numeric, 2)            AS min_winning_rate_pct,
                ROUND((MAX(b.rate) * 100)::numeric, 2)            AS max_winning_rate_pct,
                ROUND(AVG(b.amount_offered)::numeric, 0)          AS avg_winning_amount,
                ROUND(AVG(b.term_months)::numeric, 0)             AS avg_winning_term_months,
                ROUND((
                    AVG(b.rate) - (
                        SELECT AVG(b2.rate)
                        FROM marketplace.bid     b2
                        JOIN marketplace.request r2 ON r2.id = b2.request_id
                        WHERE r2.product_id = r.product_id
                          AND b2.status IN ('submitted', 'accepted')
                    )
                ) * 100, 2)                                       AS rate_vs_market_avg_pct
            FROM marketplace.request r
            JOIN marketplace.bid     b ON b.id = r.winning_bid_id
            JOIN catalog.product     p ON p.id = r.product_id
            WHERE r.accepted_at IS NOT NULL
              AND r.accepted_at > now() - interval '90 days'
            GROUP BY p.code, r.product_id
        """)).fetchall()

        competitiveness_rows = conn.execute(text("""
            SELECT
                p.code                                          AS product_code,
                COUNT(DISTINCT b.institution_id)                AS active_institutions,
                ROUND(AVG(bid_counts.n)::numeric, 1)            AS avg_bids_per_request,
                MAX(bid_counts.n)                               AS max_bids_per_request
            FROM (
                SELECT request_id, COUNT(*) AS n
                FROM marketplace.bid
                WHERE status IN ('submitted', 'accepted')
                  AND created_at > now() - interval '90 days'
                GROUP BY request_id
            ) bid_counts
            JOIN marketplace.bid     b ON b.request_id = bid_counts.request_id
            JOIN marketplace.request r ON r.id = b.request_id
            JOIN catalog.product     p ON p.id = r.product_id
            GROUP BY p.code
        """)).fetchall()

    def _clean(row) -> dict:
        d = dict(row._mapping)
        code = d.pop("product_code")
        d["product_type"] = _app_type(code)
        return d

    return {
        "marketRates":     [_clean(row) for row in rate_rows],
        "acceptanceIntel": [_clean(row) for row in acceptance_rows],
        "competitiveness": [_clean(row) for row in competitiveness_rows],
    }
