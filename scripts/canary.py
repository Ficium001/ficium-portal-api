#!/usr/bin/env python3
"""Production canary: is the live marketplace actually able to work right now?

The integration suite proves the logic against the live databases (rolled back).
It cannot see a deploy setting: in Nov 2026 the borrower -> portal event channel
answered every call with a server error for 9 days (signing keys not set on the
host) and every check stayed green. This script looks from the outside, the way
a borrower and a bank would, plus read-only queries on what the live databases
recorded. It never writes.

Outside (HTTP, no credentials):
  portal-api /health is up
  borrower -> portal events  unsigned POST -> 401 expected (503: keys not set, requests stuck)
  bid acceptance             unsigned POST -> 401 expected (503: acceptance off or keys not set)
  portal -> borrower events  unsigned POST -> 401 expected (503: keys not set, bids stuck)
  borrower app, bank portal  page loads

Inside (read-only SQL on both databases):
  App DB: integration events waiting more than 15 minutes, or failing
  both DBs: an API channel with mostly server/network errors in the last 24 h
  App DB: a scheduled job that failed in the last 24 h
  Portal DB: requests still open for bids long after their window closed

Exit 1 on any FAIL; WARN does not fail. Writes canary-report.md for the issue
the workflow opens. URLs come from env so a domain change is a repo variable.

The unsigned probes are rejected before they do anything, and show up as one
client error per run on the traffic pages (User-Agent Ficium-Canary).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

PORTAL_API = os.environ.get(
    "CANARY_PORTAL_API_URL", "https://ficium-portal-api-production.up.railway.app"
).rstrip("/")
APP_URL = os.environ.get("CANARY_APP_URL", "https://ficium.net").rstrip("/")
PORTAL_URL = os.environ.get("CANARY_PORTAL_URL", "https://ficium-portal.vercel.app").rstrip("/")
UA = {"User-Agent": "Ficium-Canary/1 (+github actions)"}
TIMEOUT = 20.0


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str


results: list[Check] = []


def record(name: str, fn: Callable[[], tuple[str, str]]) -> None:
    try:
        status, detail = fn()
    except Exception as e:  # noqa: BLE001 - a broken check is itself a failure to report
        status, detail = "FAIL", f"check could not run: {type(e).__name__}: {str(e)[:200]}"
    results.append(Check(name, status, detail))


# ── outside ──────────────────────────────────────────────────────────────────


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=False)


def health() -> tuple[str, str]:
    with _client() as c:
        r = c.get(f"{PORTAL_API}/health")
    if r.status_code == 200 and r.json().get("status") == "ok":
        return "PASS", f"up, version {r.json().get('version', '?')}"
    return "FAIL", f"HTTP {r.status_code}"


def unsigned_probe(url: str, off_means: str) -> Callable[[], tuple[str, str]]:
    def run() -> tuple[str, str]:
        with _client() as c:
            r = c.post(url, content=b"{}", headers={"Content-Type": "application/json"})
        body = r.text[:120]
        if r.status_code == 401:
            return "PASS", "on (signature required, unsigned call refused)"
        if r.status_code == 503:
            return "FAIL", f"OFF: {off_means} ({body})"
        return "FAIL", f"unexpected HTTP {r.status_code}: {body}"

    return run


def page(url: str) -> Callable[[], tuple[str, str]]:
    def run() -> tuple[str, str]:
        with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as c:
            r = c.get(url)
        return (
            ("PASS", f"HTTP {r.status_code}")
            if r.status_code < 400
            else ("FAIL", f"HTTP {r.status_code}")
        )

    return run


# ── inside ───────────────────────────────────────────────────────────────────


def _connect(env: str):  # type: ignore[no-untyped-def]
    import psycopg2
    from migrate import session_dsn

    dsn = os.environ.get(env)
    if not dsn:
        raise RuntimeError(f"{env} not set")
    conn = psycopg2.connect(session_dsn(dsn), connect_timeout=15)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def outbox_backlog() -> tuple[str, str]:
    with _connect("APP_DB_DSN") as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE delivered_at IS NULL
                                      AND created_at < now() - interval '15 minutes'),
                   count(*) FILTER (WHERE delivered_at IS NULL AND attempts >= 3),
                   max(last_error) FILTER (WHERE delivered_at IS NULL)
              FROM integration.outbox
        """)
        stuck, failing, err = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM integration.emit_error WHERE at > now() - interval '24 hours'"
        )
        (emit_errors,) = cur.fetchone()
    if stuck or failing:
        return (
            "FAIL",
            f"{stuck} event(s) waiting > 15 min, {failing} failing; "
            f"last error: {str(err)[:160]}",
        )
    if emit_errors:
        return (
            "WARN",
            f"{emit_errors} event(s) could not be built in the last 24 h "
            "(integration.emit_error)",
        )
    return "PASS", "nothing waiting"


TRAFFIC_SQL = """
    SELECT channel, sum(total)::int, sum(server_errors + network_errors)::int
      FROM {table}
     WHERE hour > now() - interval '24 hours'
     GROUP BY channel
    HAVING sum(total) >= 3 AND sum(server_errors + network_errors) * 2 >= sum(total)
     ORDER BY 1
"""


def traffic(env: str, table: str) -> Callable[[], tuple[str, str]]:
    def run() -> tuple[str, str]:
        with _connect(env) as conn, conn.cursor() as cur:
            cur.execute(TRAFFIC_SQL.format(table=table))
            bad = cur.fetchall()
        if bad:
            return "FAIL", "; ".join(f"{ch}: {err}/{tot} calls failed" for ch, tot, err in bad)
        return "PASS", "no channel mostly failing"

    return run


def cron_failures() -> tuple[str, str]:
    with _connect("APP_DB_DSN") as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT j.jobname, count(*), max(left(d.return_message, 120))
              FROM cron.job_run_details d JOIN cron.job j ON j.jobid = d.jobid
             WHERE d.status = 'failed' AND d.start_time > now() - interval '24 hours' AND j.active
             GROUP BY j.jobname ORDER BY 1
        """)
        rows = cur.fetchall()
    if rows:
        return "FAIL", "; ".join(f"{n} failed {c}x ({m})" for n, c, m in rows)
    return "PASS", "no failed runs"


def stale_windows() -> tuple[str, str]:
    with _connect("PORTAL_DB_DSN") as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM marketplace.request
             WHERE status = 'bidding' AND bid_window_closes_at < now() - interval '2 hours'
        """)
        (n,) = cur.fetchone()
    if n:
        return "WARN", (
            f"{n} request(s) still open for bids 2 h after their window closed "
            "(is MAINTENANCE_CLOSE_EXPIRED_ENABLED on?)"
        )
    return "PASS", "none"


def main() -> int:
    record("portal-api is up", health)
    record(
        "Borrower requests reach banks (events in)",
        unsigned_probe(
            f"{PORTAL_API}/integration/v1/events",
            "INTEGRATION_B2I_VERIFY_KEYS not set on portal-api; "
            "new requests never reach banks",
        ),
    )
    record(
        "Borrowers can accept bids",
        unsigned_probe(
            f"{PORTAL_API}/integration/v1/acceptances",
            "INTEGRATION_ACCEPTANCE_ENABLED off or keys not set on portal-api",
        ),
    )
    record(
        "Bids reach borrowers (events to the app)",
        unsigned_probe(
            f"{APP_URL}/api/internal?op=events",
            "verify keys not set on the borrower app; bids never reach borrowers",
        ),
    )
    record("Borrower app loads", page(APP_URL))
    record("Bank portal loads", page(PORTAL_URL))
    record("App DB: integration events delivered", outbox_backlog)
    record("App DB: API channels healthy (24 h)", traffic("APP_DB_DSN", "admin.api_traffic_hour"))
    record(
        "Portal DB: API channels healthy (24 h)",
        traffic("PORTAL_DB_DSN", "integration.api_traffic_hour"),
    )
    record("App DB: scheduled jobs (24 h)", cron_failures)
    record("Portal DB: bid windows close on time", stale_windows)

    icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    lines = ["| | Check | Result |", "|---|---|---|"]
    lines += [f"| {icon[r.status]} | {r.name} | {r.detail.replace('|', '/')} |" for r in results]
    report = "\n".join(lines)
    Path("canary-report.md").write_text(report + "\n")
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Production canary\n\n" + report + "\n")
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
