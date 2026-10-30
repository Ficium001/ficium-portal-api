"""
Nightly reconciliation: do the borrower app (App DB) and the bank portal (portal DB)
agree about every request, bid and deal?

Read-only on both databases. Flags only drift that misleads someone:

  R1  request exists on one side only
  R2  accepted on one side, not the other
  R3  open for bids on one side, finished on the other
  R4  the bank declined/cancelled the deal, the borrower still sees "accepted"
  R5  the deal completed at the bank, the borrower's tracker has nothing
  R6  the App's winning bid is missing or differs from the portal's accepted bid
  R7  a bid is still live on a request that is no longer taking bids

Known, already-decided cases go in scripts/reconcile_acknowledged.json as
{"<rule>:<id>": "<who decided what, when>"} so they stop alerting but stay visible.
Exit 1 (and a GitHub error annotation per finding) when anything new is found.

Run: PORTAL_DB_DSN=... APP_DB_DSN=... python scripts/reconcile.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

APP_OPEN = {"open", "awaiting_consent", "PENDING"}
PORTAL_OPEN = {"bidding", "open"}
PIPE_CLOSED_BAD = {"declined", "cancelled", "withdrawn"}
PIPE_DONE = {"completed", "booked", "disbursed"}
BID_LIVE = {"submitted", "pending_approval"}


def _dsn(v: str) -> str:
    return v.replace("postgres://", "postgresql+psycopg2://", 1).replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )


def _rows(dsn: str, sql: str) -> list[dict[str, Any]]:
    eng = create_engine(_dsn(dsn), pool_size=1, max_overflow=0)
    try:
        with eng.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            return [dict(r._mapping) for r in c.execute(text(sql))]
    finally:
        eng.dispose()


def find(app: dict[str, Any], portal: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Pure comparison (unit-tested): returns (rule, id, message)."""
    out: list[tuple[str, str, str]] = []
    a_req = {r["id"]: r for r in app["requests"]}
    p_req = {r["id"]: r for r in portal["requests"]}
    p_acc_bid = {b["request_id"]: b["id"] for b in portal["bids"] if b["status"] == "accepted"}
    p_pipe = {p["request_id"]: p for p in portal["pipelines"]}
    a_prog = {d["request_id"] for d in app["deal_progress"]}

    for rid in sorted(set(a_req) - set(p_req)):
        if a_req[rid]["status"] not in APP_OPEN | {"expired", "closed", "rejected"}:
            out.append(("R1", rid, f"in the App ({a_req[rid]['status']}) but not on the portal"))
    for rid in sorted(set(p_req) - set(a_req)):
        out.append(("R1", rid, f"on the portal ({p_req[rid]['status']}) but not in the App"))

    for rid in sorted(set(a_req) & set(p_req)):
        a, p = a_req[rid]["status"], p_req[rid]["status"]
        if (a == "accepted") != (p == "accepted"):
            out.append(("R2", rid, f"App says '{a}', portal says '{p}'"))
        elif (a in APP_OPEN) != (p in PORTAL_OPEN):
            out.append(("R3", rid, f"App says '{a}', portal says '{p}'"))
        pipe = p_pipe.get(rid)
        if a == "accepted" and pipe and pipe["status"] in PIPE_CLOSED_BAD:
            out.append(
                ("R4", rid, f"bank {pipe['status']} the deal; borrower still sees 'accepted'")
            )
        if pipe and pipe["status"] in PIPE_DONE and rid not in a_prog:
            out.append(
                ("R5", rid, f"deal {pipe['status']} at the bank; borrower's tracker is empty")
            )
        if a == "accepted":
            want, got = p_acc_bid.get(rid), a_req[rid]["accepted_bid_id"]
            if want and got != want:
                out.append(
                    ("R6", rid, f"App winning bid {got or 'missing'}, portal accepted {want}")
                )

    for b in portal["bids"]:
        req = p_req.get(b["request_id"])
        if b["status"] in BID_LIVE and req and req["status"] not in PORTAL_OPEN:
            out.append(("R7", b["id"], f"bid still '{b['status']}' on a '{req['status']}' request"))
    return out


def main() -> int:
    app_dsn, portal_dsn = os.environ.get("APP_DB_DSN"), os.environ.get("PORTAL_DB_DSN")
    if not app_dsn or not portal_dsn:
        print("::error::APP_DB_DSN and PORTAL_DB_DSN are required (never skip silently)")
        return 2
    app = {
        "requests": _rows(
            app_dsn, "SELECT id::text, status::text, accepted_bid_id::text FROM public.requests"
        ),
        "deal_progress": _rows(app_dsn, "SELECT request_id::text FROM integration.deal_progress"),
    }
    portal = {
        "requests": _rows(portal_dsn, "SELECT id::text, status::text FROM marketplace.request"),
        "bids": _rows(
            portal_dsn, "SELECT id::text, request_id::text, status::text FROM marketplace.bid"
        ),
        "pipelines": _rows(
            portal_dsn, "SELECT request_id::text, status::text FROM marketplace.loan_pipeline"
        ),
    }
    ack_file = Path(__file__).with_name("reconcile_acknowledged.json")
    ack: dict[str, str] = json.loads(ack_file.read_text()) if ack_file.exists() else {}

    findings = find(app, portal)
    new = [f for f in findings if f"{f[0]}:{f[1]}" not in ack]
    print(
        f"compared {len(app['requests'])} App / {len(portal['requests'])} portal requests, "
        f"{len(portal['bids'])} bids, {len(portal['pipelines'])} deals: "
        f"{len(findings)} finding(s), {len(findings) - len(new)} acknowledged"
    )
    for rule, rid, msg in findings:
        tag = "acknowledged" if f"{rule}:{rid}" in ack else "NEW"
        print(f"  [{tag}] {rule} {rid}: {msg}")
    for rule, rid, msg in new:
        print(f"::error title=Reconciliation {rule}::{rid}: {msg}")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
