"""GET /marketplace/intelligence: shaping and competitor-figure suppression."""
from __future__ import annotations

from app.core.market_intelligence import MIN_INSTITUTIONS, MIN_REQUESTS, shape


def _built(active: int) -> dict:
    return {
        "as_of": "2026-11-26",
        "rates": [{"product_code": "personal_loan", "bid_count": 9, "request_count": 4,
                   "min_rate": 0.065, "max_rate": 0.11, "avg_rate": 0.0825,
                   "p25_rate": 0.07, "p75_rate": 0.095}],
        "outcomes": [{"product_code": "personal_loan", "total_acceptances": 2,
                      "avg_winning_rate": 0.071, "min_winning_rate": 0.065,
                      "avg_winning_amount": 500000, "avg_winning_term_months": 60,
                      "rate_vs_market_avg": -0.0115}],
        "competitiveness": [{"product_code": "personal_loan", "active_institutions": active,
                             "avg_bids_per_request": 2.3, "max_bids_per_request": 4}],
    }


def test_rates_converted_to_percent_when_enough_competitors():
    out = shape(_built(MIN_INSTITUTIONS), [])
    (r,) = out["marketRates"]
    assert r["product_type"] == "personal_loan"
    assert r["avg_rate_pct"] == 8.25
    assert r["p25_rate_pct"] == 7.0
    (a,) = out["acceptanceIntel"]
    assert a["avg_winning_rate_pct"] == 7.1
    assert a["rate_vs_market_avg_pct"] == -1.15
    assert out["competitiveness"][0]["avg_bids_per_request"] == 2.3


def test_competitor_figures_suppressed_below_threshold():
    out = shape(_built(MIN_INSTITUTIONS - 1), [])
    assert out["marketRates"] == []
    assert out["acceptanceIntel"] == []
    assert out["competitiveness"] == []
    assert "Not enough competing institutions" in out["summary"]


def test_demand_suppressed_below_min_requests():
    rows = [
        {"product_type": "home_loan", "total_requests": MIN_REQUESTS, "open_requests": 2,
         "avg_amount": 3_000_000.4, "median_amount": 2_500_000, "avg_term_months": 240,
         "close_rate_pct": 20.0},
        {"product_type": "car_loan", "total_requests": MIN_REQUESTS - 1, "open_requests": 1,
         "avg_amount": 1, "median_amount": 1, "avg_term_months": 1, "close_rate_pct": 0},
    ]
    out = shape({}, rows)
    assert [p["product_type"] for p in out["requestPatterns"]] == ["home_loan"]
    assert out["requestPatterns"][0]["avg_amount"] == 3_000_000


def test_empty_market_has_frontend_shape():
    out = shape({}, [])
    for k in ("generatedAt", "marketRates", "requestPatterns", "acceptanceIntel",
              "competitiveness", "summary"):
        assert k in out

