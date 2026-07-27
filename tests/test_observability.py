# =============================================================================
# Unit tests — app/core/observability.py
#
# DB-free. Covers the metrics registry (counters, gauges, histogram bucket
# math, Prometheus text rendering), request-ID propagation, route-template
# label cardinality, and 5xx counting when a handler raises.
# =============================================================================

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.observability import (
    DEFAULT_BUCKETS,
    MetricsRegistry,
    RequestObservabilityMiddleware,
    metrics,
    metrics_endpoint,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_counter_accumulates_per_label_set() -> None:
    r = MetricsRegistry()
    r.describe("t_total", "counter", "test")
    r.inc("t_total", {"route": "/a"})
    r.inc("t_total", {"route": "/a"})
    r.inc("t_total", {"route": "/b"})
    out = r.render()
    assert 't_total{route="/a"} 2' in out
    assert 't_total{route="/b"} 1' in out


def test_gauge_sets_latest_value() -> None:
    r = MetricsRegistry()
    r.describe("t_gauge", "gauge", "test")
    r.set_gauge("t_gauge", 5)
    r.set_gauge("t_gauge", 3)
    assert "t_gauge 3" in r.render()


def test_histogram_buckets_are_cumulative_and_sum_correct() -> None:
    r = MetricsRegistry()
    r.describe("t_seconds", "histogram", "test")
    r.observe("t_seconds", 0.004)   # le 0.005
    r.observe("t_seconds", 0.09)    # le 0.1
    r.observe("t_seconds", 99.0)    # +Inf overflow
    out = r.render()
    assert 't_seconds_bucket{le="0.005"} 1' in out
    assert 't_seconds_bucket{le="0.1"} 2' in out
    assert 't_seconds_bucket{le="30"} 2' in out          # 99s beyond last bound
    assert 't_seconds_bucket{le="+Inf"} 3' in out
    assert "t_seconds_count 3" in out
    assert "t_seconds_sum 99.094" in out


def test_bucket_bounds_are_sorted() -> None:
    assert list(DEFAULT_BUCKETS) == sorted(DEFAULT_BUCKETS)


def test_render_includes_help_and_type() -> None:
    r = MetricsRegistry()
    r.describe("x_total", "counter", "The x.")
    r.inc("x_total")
    out = r.render()
    assert "# HELP x_total The x." in out
    assert "# TYPE x_total counter" in out


# ---------------------------------------------------------------------------
# Middleware behaviour
# ---------------------------------------------------------------------------

@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestObservabilityMiddleware)
    app.add_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

    @app.get("/items/{item_id}")
    def get_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaput")

    metrics.reset()
    return TestClient(app, raise_server_exceptions=False)


def test_request_id_minted_and_echoed(client: TestClient) -> None:
    r = client.get("/items/1")
    assert r.status_code == 200
    assert len(r.headers["x-request-id"]) == 32


def test_inbound_request_id_propagated(client: TestClient) -> None:
    r = client.get("/items/1", headers={"x-request-id": "portal-abc-123"})
    assert r.headers["x-request-id"] == "portal-abc-123"


def test_metrics_use_route_template_not_raw_path(client: TestClient) -> None:
    client.get("/items/1")
    client.get("/items/2")
    client.get("/items/3")
    body = client.get("/metrics").text
    # One bounded label series, not one per item id
    assert 'route="/items/{item_id}"' in body
    assert 'route="/items/1"' not in body
    assert (
        'fic_http_requests_total{method="GET",route="/items/{item_id}",status="200"} 3' in body
    )


def test_unhandled_exception_counted_as_500(client: TestClient) -> None:
    r = client.get("/boom")
    assert r.status_code == 500
    body = client.get("/metrics").text
    assert 'route="/boom",status="500"} 1' in body


def test_in_flight_gauge_returns_to_zero(client: TestClient) -> None:
    client.get("/items/1")
    body = client.get("/metrics").text
    # The /metrics request itself is in flight while rendering → gauge shows 1
    assert "fic_http_requests_in_flight 1" in body
