# =============================================================================
# ficium-portal-api — Observability core (plat:observability)
#
# Platform infrastructure, not a feature of any one module. Three concerns:
#
#   configure_logging()        structlog → single-line JSON to stdout in
#                              production (Railway log drains parse it),
#                              pretty console in dev. Every log line carries
#                              request_id / method / path via contextvars,
#                              so one grep reconstructs a request's story.
#
#   RequestObservabilityMiddleware
#                              per-request: generates/propagates
#                              X-Request-Id, binds structlog context, times
#                              the request, records metrics, emits one
#                              canonical http_request log line.
#
#   metrics registry           dependency-free counters + histograms exposed
#                              in Prometheus text format at GET /metrics.
#                              Vendor-neutral: Grafana Cloud / Prometheus /
#                              Railway metrics all scrape this. OTLP traces
#                              are a deliberate follow-up — logs + RED
#                              metrics first, spans second.
#
# Alerting contract (wire in Grafana): alert on
#   rate(fic_http_requests_total{status=~"5.."}[5m]) and
#   fic_worker_heartbeat_age_seconds — the worker-side gauges are
#   registered here so the autobid worker inherits the same registry.
# =============================================================================

from __future__ import annotations

import logging
import math
import sys
import time
import uuid
from collections import defaultdict
from threading import Lock
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(*, env: str, level: str = "INFO") -> None:
    """
    Configure structlog once at startup. JSON in production, console in dev.
    Idempotent — safe to call from tests.
    """
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if env == "production"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Metrics registry (dependency-free, Prometheus text exposition format)
# ---------------------------------------------------------------------------

# Latency buckets in seconds — tuned for an API with a 30s DB timeout.
DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

_LabelKey = tuple[tuple[str, str], ...]


def _label_key(labels: dict[str, str]) -> _LabelKey:
    return tuple(sorted(labels.items()))


def _fmt_labels(key: _LabelKey, extra: str = "") -> str:
    parts = [f'{k}="{v}"' for k, v in key]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class MetricsRegistry:
    """
    Minimal thread-safe counters / gauges / histograms.

    Deliberately in-process and unlabeled-cardinality-bounded: callers must
    keep label values low-cardinality (route templates, not raw paths;
    module codes, not institution IDs). Institution-level analytics belong
    in the database, not in metrics.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, dict[_LabelKey, float]] = defaultdict(dict)
        self._gauges: dict[str, dict[_LabelKey, float]] = defaultdict(dict)
        self._hist_counts: dict[str, dict[_LabelKey, list[int]]] = defaultdict(dict)
        self._hist_sums: dict[str, dict[_LabelKey, float]] = defaultdict(dict)
        self._help: dict[str, tuple[str, str]] = {}  # name → (type, help)

    def describe(self, name: str, mtype: str, help_text: str) -> None:
        self._help[name] = (mtype, help_text)

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1) -> None:
        key = _label_key(labels or {})
        with self._lock:
            self._counters[name][key] = self._counters[name].get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = _label_key(labels or {})
        with self._lock:
            self._gauges[name][key] = value

    def add_gauge(self, name: str, delta: float, labels: dict[str, str] | None = None) -> None:
        key = _label_key(labels or {})
        with self._lock:
            self._gauges[name][key] = self._gauges[name].get(key, 0.0) + delta

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = _label_key(labels or {})
        with self._lock:
            if key not in self._hist_counts[name]:
                self._hist_counts[name][key] = [0] * (len(DEFAULT_BUCKETS) + 1)
                self._hist_sums[name][key] = 0.0
            counts = self._hist_counts[name][key]
            for i, bound in enumerate(DEFAULT_BUCKETS):
                if value <= bound:
                    counts[i] += 1
                    break
            else:
                counts[-1] += 1
            self._hist_sums[name][key] += value

    def render(self) -> str:
        """Prometheus text exposition format 0.0.4."""
        out: list[str] = []
        with self._lock:
            for name, (mtype, help_text) in sorted(self._help.items()):
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} {mtype}")
                if mtype == "counter":
                    for key, v in sorted(self._counters.get(name, {}).items()):
                        out.append(f"{name}{_fmt_labels(key)} {_num(v)}")
                elif mtype == "gauge":
                    for key, v in sorted(self._gauges.get(name, {}).items()):
                        out.append(f"{name}{_fmt_labels(key)} {_num(v)}")
                elif mtype == "histogram":
                    for key, counts in sorted(self._hist_counts.get(name, {}).items()):
                        cumulative = 0
                        for i, bound in enumerate(DEFAULT_BUCKETS):
                            cumulative += counts[i]
                            le = f'le="{_num(bound)}"'
                            out.append(f"{name}_bucket{_fmt_labels(key, le)} {cumulative}")
                        cumulative += counts[-1]
                        out.append(f"{name}_bucket{_fmt_labels(key, 'le=\"+Inf\"')} {cumulative}")
                        out.append(f"{name}_count{_fmt_labels(key)} {cumulative}")
                        total = _num(self._hist_sums[name][key])
                        out.append(f"{name}_sum{_fmt_labels(key)} {total}")
        return "\n".join(out) + "\n"

    def reset(self) -> None:
        """Testing hook."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist_counts.clear()
            self._hist_sums.clear()


def _num(v: float) -> str:
    if math.isinf(v):
        return "+Inf"
    if float(v).is_integer():
        return str(int(v))
    return repr(float(v))


# Global registry: one per process, shared by API and (later) workers.
metrics = MetricsRegistry()
metrics.describe("fic_http_requests_total", "counter", "HTTP requests by route/method/status.")
metrics.describe(
    "fic_http_request_duration_seconds", "histogram", "HTTP request latency by route/method."
)
metrics.describe("fic_http_requests_in_flight", "gauge", "In-flight HTTP requests.")
metrics.describe(
    "fic_autobid_executions_total", "counter", "Auto-bid evaluations by outcome (worker)."
)
metrics.describe(
    "fic_worker_heartbeat_timestamp_seconds", "gauge", "Unix time of last worker loop (worker)."
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Request-ID propagation + canonical log line + RED metrics.

    Accepts an inbound X-Request-Id (so IDs flow portal → api → worker),
    otherwise mints one. Uses the ROUTE TEMPLATE (not the raw path) as the
    metrics label to keep cardinality bounded.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        metrics.add_gauge("fic_http_requests_in_flight", 1)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - start
            metrics.add_gauge("fic_http_requests_in_flight", -1)
            route = request.scope.get("route")
            route_label = getattr(route, "path", request.url.path)
            labels = {"route": route_label, "method": request.method, "status": str(status)}
            metrics.inc("fic_http_requests_total", labels)
            metrics.observe(
                "fic_http_request_duration_seconds",
                elapsed,
                {"route": route_label, "method": request.method},
            )
            structlog.get_logger("http").info(
                "http_request", status=status, duration_ms=round(elapsed * 1000, 2)
            )
            structlog.contextvars.clear_contextvars()


def metrics_endpoint(_: Request) -> PlainTextResponse:
    """GET /metrics — Prometheus scrape target. No auth: exposes no tenant data."""
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")
