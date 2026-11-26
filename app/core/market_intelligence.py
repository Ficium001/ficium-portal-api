"""Market intelligence for the bank portal: anonymous aggregates, shaped for the portal UI.

The numbers come from integration.build_market_intelligence() (the same function that feeds
the borrower side's market.intelligence event) plus per-product request demand. This module
only reshapes and suppresses; it never queries.

Suppression: a product's competitor figures (rates, winning rates, competitiveness) are shown
only when at least MIN_INSTITUTIONS distinct institutions bid on it in the window. Below that, an
"average rate" is one or two named competitors' prices, which a bank could work out. Demand
figures (how many requests, typical amount) describe borrowers in aggregate, not competitors,
and are suppressed only below MIN_REQUESTS.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

MIN_INSTITUTIONS = 3
MIN_REQUESTS = 5


def _pct(v: Any) -> float | None:
    if v is None:
        return None
    return round(float(v) * 100, 2)


def _num(v: Any) -> float:
    return float(v) if v is not None else 0.0


def shape(built: dict[str, Any], demand: list[dict[str, Any]]) -> dict[str, Any]:
    """build_market_intelligence() output + demand rows -> the portal's FiciumIntelligence."""
    comp_rows = built.get("competitiveness") or []
    eligible = {
        c["product_code"] for c in comp_rows
        if c.get("product_code") and int(c.get("active_institutions") or 0) >= MIN_INSTITUTIONS
    }

    market_rates = [
        {
            "product_type": r["product_code"],
            "bid_count": int(r.get("bid_count") or 0),
            "request_count": int(r.get("request_count") or 0),
            "min_rate_pct": _pct(r.get("min_rate")),
            "max_rate_pct": _pct(r.get("max_rate")),
            "avg_rate_pct": _pct(r.get("avg_rate")),
            "p25_rate_pct": _pct(r.get("p25_rate")),
            "p75_rate_pct": _pct(r.get("p75_rate")),
        }
        for r in built.get("rates") or []
        if r.get("product_code") in eligible and r.get("avg_rate") is not None
    ]

    acceptance = [
        {
            "product_type": o["product_code"],
            "total_acceptances": int(o.get("total_acceptances") or 0),
            "avg_winning_rate_pct": _pct(o.get("avg_winning_rate")),
            "min_winning_rate_pct": _pct(o.get("min_winning_rate")),
            "avg_winning_amount": _num(o.get("avg_winning_amount")),
            "avg_winning_term_months": int(o.get("avg_winning_term_months") or 0),
            "rate_vs_market_avg_pct": _pct(o.get("rate_vs_market_avg")),
        }
        for o in built.get("outcomes") or []
        if o.get("product_code") in eligible
    ]

    competitiveness = [
        {
            "product_type": c["product_code"],
            "active_institutions": int(c.get("active_institutions") or 0),
            "avg_bids_per_request": _num(c.get("avg_bids_per_request")),
        }
        for c in comp_rows
        if c.get("product_code") in eligible
    ]

    patterns = [
        {
            "product_type": d["product_type"],
            "total_requests": int(d.get("total_requests") or 0),
            "open_requests": int(d.get("open_requests") or 0),
            "avg_amount": round(_num(d.get("avg_amount")), 0),
            "median_amount": round(_num(d.get("median_amount")), 0),
            "avg_term_months": round(_num(d.get("avg_term_months")), 1),
            "close_rate_pct": round(_num(d.get("close_rate_pct")), 1),
        }
        for d in demand
        if d.get("product_type") and int(d.get("total_requests") or 0) >= MIN_REQUESTS
    ]

    shown = len(market_rates)
    summary = (
        f"Rates across {shown} product{'s' if shown != 1 else ''} with at least "
        f"{MIN_INSTITUTIONS} competing institutions, last 90 days."
        if shown else
        "Not enough competing institutions yet to show market rates "
        f"(needs {MIN_INSTITUTIONS} per product)."
    )
    return {
        "generatedAt": datetime.now(UTC).isoformat(),
        "marketRates": market_rates,
        "requestPatterns": patterns,
        "acceptanceIntel": acceptance,
        "competitiveness": competitiveness,
        "summary": summary,
    }
