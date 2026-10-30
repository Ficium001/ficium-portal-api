"""
Golden path, both sides: one deal from the borrower's app to the bank's core system,
with the BORROWER side real too.

test_golden_path.py proves the bank side and fakes the borrower (hand-written
envelopes). This test replaces every fake with the App DB's own code:

    App DB: a borrower and a request are created; the App DB's trigger writes the
            request.published envelope                      -> sent to the portal API
    Portal: bid, four eyes                                  -> bid.placed envelope
    App DB: the borrower's inbox applies it (integration_apply_bid_event);
            the borrower can see the offer (integration_bids_for)
    App DB: integration_prepare_acceptance builds the consent + released identity
    Portal: acceptance call with exactly that body          -> pipeline opens
    App DB: integration_complete_acceptance records it; request accepted
    Portal: every stage, documents, credit approval, booking
    App DB: every deal event the portal queued is applied; the borrower's tracker
            shows the deal and its documents

Only the TypeScript glue in the borrower app (HTTP routing, signing) is not run
here; it has its own unit tests. Every SQL function it calls is.

Nothing persists: the portal side runs in the golden-path transaction (rolled
back); the App DB side runs in its own transaction on APP_DB_DSN (rolled back).
No HTTP call leaves the test.

Run: PORTAL_DB_DSN=... APP_DB_DSN=... pytest tests/test_golden_path_two_sides.py -v -s
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import ficium_contract as fc
import pytest
from sqlalchemy import create_engine, text

from tests.test_golden_path import (  # noqa: F401  (journey is a fixture)
    ACCEPT_KEY,
    B2I_KEY,
    MCB,
    Journey,
    _person,
    _sqlalchemy_dsn,
    journey,
    ok,
    work_deal,
)
from tests.test_integration import APP_DSN, requires_app, requires_portal

pytestmark = [requires_portal, requires_app]
NAME = "Two Sides Borrower"

# The borrower app's appliers (api/_lib/integration/supabase.ts in ficium): one RPC per type.
APP_APPLIERS = {
    "bid.placed": "integration_apply_bid_event",
    "bid.updated": "integration_apply_bid_event",
    "bid.withdrawn": "integration_apply_bid_event",
    "deal.progress_updated": "integration_apply_deal_progress_event",
    "deal.documents_updated": "integration_apply_deal_documents_event",
    "request.window_closed": "integration_apply_request_window_closed_event",
    "request.declined": "integration_apply_request_declined_event",
}


class AppSide:
    """The App DB in one transaction that is always rolled back."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.applied: list[tuple[str, str]] = []

    def sql(self, q: str, p: dict[str, Any] | None = None) -> list[Any]:
        r = self.conn.execute(text(q), p or {})
        return list(r.fetchall()) if r.returns_rows else []

    def one(self, q: str, p: dict[str, Any] | None = None) -> Any:
        rows = self.sql(q, p)
        return rows[0][0] if rows else None

    def deliver(self, env: dict[str, Any]) -> str:
        """What the app's /api/integration receiver does with a verified envelope."""
        fc.validate_event(env)
        fn = APP_APPLIERS.get(env["type"])
        assert fn, f"the borrower app has no receiver for '{env['type']}'"
        outcome = self.one(f"SELECT {fn}(CAST(:e AS jsonb))", {"e": json.dumps(env)})
        self.applied.append((env["type"], str(outcome)))
        return str(outcome)


@pytest.fixture()
def app_side() -> Iterator[AppSide]:
    engine = create_engine(_sqlalchemy_dsn(APP_DSN or ""), pool_size=1, max_overflow=0)
    conn = engine.connect()
    tx = conn.begin()
    try:
        yield AppSide(conn)
    finally:
        tx.rollback()
        conn.close()
        engine.dispose()


def _portal_events(j: Journey, *aggregates: str, since: set[str]) -> list[dict[str, Any]]:
    """Envelopes the portal queued for the borrower app, oldest first per aggregate."""
    rows = j.sql(
        """SELECT id, envelope FROM integration.outbox
            WHERE aggregate_id = ANY(:a) AND NOT (id = ANY(:seen))
            ORDER BY aggregate_id, sequence""",
        {"a": list(aggregates), "seen": list(since)},
    )
    since.update(r[0] for r in rows)
    return [r[1] for r in rows]


def test_golden_path_two_sides(journey: Journey, app_side: AppSide) -> None:  # noqa: F811
    j, app = journey, app_side
    _person(j, "officer", "bank_officer")
    _person(j, "approver", "bank_officer_approver")
    _person(j, "admin", "institution_admin")
    seen: set[str] = set()

    if not j.sql(
        "SELECT can_bid FROM institution.compliance WHERE institution_id = CAST(:i AS uuid)",
        {"i": MCB},
    )[0][0]:
        j.say("0. MCB has no approved compliance documents: adding them for this run only")
        j.sql(
            """INSERT INTO institution.doc
                   (institution_id, doc_type_id, storage_path, file_name, mime_type,
                    status, uploaded_at)
                 SELECT CAST(:i AS uuid), dt.id, 'golden/' || dt.code || '.pdf', dt.code || '.pdf',
                        'application/pdf', 'approved', now()
                   FROM institution.doc_type dt
                  WHERE dt.code = ANY (SELECT unnest(missing_docs) FROM institution.compliance
                                        WHERE institution_id = CAST(:i AS uuid))""",
            {"i": MCB},
        )

    # 1 ── A borrower in the App DB creates a request ────────────────────────────
    j.say("1. App DB: a KYC-verified borrower creates a personal loan request")
    client_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    app.sql(
        """INSERT INTO public.clients (id, email, full_name, user_type, kyc_status, phone)
           VALUES (CAST(:c AS uuid), :e, 'Two Sides Borrower', 'individual', 'verified',
                   '+230 5555 0001')""",
        {"c": client_id, "e": f"two.sides.{client_id[:8]}@example.com"},
    )
    app.sql(
        """INSERT INTO public.requests (id, client_id, product_type, amount, preferred_term_months,
                                       purpose, decision_deadline, status)
           VALUES (CAST(:r AS uuid), CAST(:c AS uuid), 'personal_loan', 400000, 48,
                   'Home renovation', now() + interval '7 days', 'open')""",
        {"r": request_id, "c": client_id},
    )
    err = app.sql(
        "SELECT stage, error FROM integration.emit_error WHERE request_id = CAST(:r AS uuid)",
        {"r": request_id},
    )
    assert not err, f"1: the App DB could not build the request event: {err}"
    published = app.one(
        """SELECT envelope FROM integration.outbox
            WHERE aggregate_id = :r AND type = 'request.published'
            ORDER BY sequence DESC LIMIT 1""",
        {"r": request_id},
    )
    assert published, "1: the App DB queued no request.published event"
    fc.validate_event(published)
    anon_id = published["data"]["anon_borrower_id"]
    assert NAME not in json.dumps(published), "1: the request event names the borrower"

    j.say("   the App DB's own envelope is delivered to the portal")
    ok(j.signed("/integration/v1/events", published, B2I_KEY), "publish request", 200, 202)
    status = j.sql(
        "SELECT status FROM marketplace.request WHERE id = CAST(:r AS uuid)", {"r": request_id}
    )
    assert status and status[0][0] == "bidding", f"1: not open for bids on the portal: {status}"

    # 2 ── Bid, four eyes ─────────────────────────────────────────────────────────
    j.say("2. Officer bids; a second person approves")
    sub = ok(
        j.call(
            "officer",
            "POST",
            "/marketplace/bids",
            json={
                "request_id": request_id,
                "rate": 0.0675,
                "rate_type": "fixed",
                "amount_offered": 400000,
                "term_months": 48,
            },
        ),
        "submit bid",
    )
    assert sub["status"] == "pending_approval", sub
    ok(
        j.call("approver", "POST", f"/approvals/{sub['action_id']}/approve", json={"note": "ok"}),
        "approve bid",
    )
    bid_id = str(
        j.sql(
            """SELECT id FROM marketplace.bid
                WHERE request_id = CAST(:r AS uuid) AND institution_id = CAST(:i AS uuid)""",
            {"r": request_id, "i": MCB},
        )[0][0]
    )

    # 3 ── The borrower's app receives the offer ────────────────────────────────
    j.say("3. App DB: the borrower's inbox applies bid.placed; the offer is visible")
    bid_events = _portal_events(j, bid_id, request_id, since=seen)
    assert any(e["type"] == "bid.placed" for e in bid_events), (
        f"3: the portal queued no bid.placed (got {[e['type'] for e in bid_events]})"
    )
    for env in bid_events:
        assert app.deliver(env) in ("apply", "duplicate", "stale"), env["type"]
    offers = app.one(
        "SELECT integration_bids_for(CAST(:c AS uuid), ARRAY[CAST(:r AS uuid)])",
        {"c": client_id, "r": request_id},
    )
    assert bid_id in json.dumps(offers), f"3: the borrower can't see the offer: {offers}"
    assert NAME not in json.dumps(bid_events), "3: a bank-side event names the borrower"

    # 4 ── Acceptance, prepared and completed by the App DB ─────────────────────
    j.say("4. App DB prepares consent + released identity; the portal accepts it")
    prep = app.one(
        """SELECT integration_prepare_acceptance(
                  CAST(:c AS uuid), CAST(:r AS uuid), CAST(:b AS uuid))""",
        {"c": client_id, "r": request_id, "b": bid_id},
    )
    assert prep and prep["anon_borrower_id"] == anon_id, f"4: consent for another borrower: {prep}"
    body = {
        "bid_id": bid_id,
        "request_id": request_id,
        "anon_borrower_id": prep["anon_borrower_id"],
        "consent_ref": prep["consent_ref"],
        "released_identity": prep["released_identity"],
    }
    fc.validate_acceptance_request(body)
    idem = f"two-sides-{uuid.uuid4()}"
    first = ok(
        j.signed("/integration/v1/acceptances", body, ACCEPT_KEY, **{"Idempotency-Key": idem}),
        "accept",
    )
    fc.validate_acceptance_response(first)
    assert first["institution"]["institution_id"] == MCB
    pipeline_id = first["pipeline_id"]
    app.sql(
        "SELECT integration_complete_acceptance(:ref, 'accepted', CAST(:i AS uuid))",
        {"ref": prep["consent_ref"], "i": MCB},
    )
    state = app.sql(
        """SELECT status::text, accepted_bid_id::text FROM public.requests
            WHERE id = CAST(:r AS uuid)""",
        {"r": request_id},
    )[0]
    assert state == ("accepted", bid_id), f"4: App DB didn't record the acceptance: {state}"
    rel = app.one(
        "SELECT outcome FROM integration.identity_release WHERE consent_ref = :ref",
        {"ref": prep["consent_ref"]},
    )
    assert rel == "accepted", f"4: consent audit is '{rel}'"

    rev = ok(j.call("officer", "GET", f"/marketplace/bids/{bid_id}/reveal"), "reveal")
    assert NAME in json.dumps(rev), f"4: identity not revealed to the winner: {rev}"

    # 5 ── The bank works the deal ───────────────────────────────────────────────
    j.say("5. MCB works every stage of the deal")
    work_deal(j, pipeline_id, anon_id)
    ok(
        j.call(
            "officer",
            "POST",
            f"/pipelines/{pipeline_id}/booking",
            json={
                "core_reference": f"TWO-SIDES-{uuid.uuid4().hex[:8]}",
                "booked_on": date.today().isoformat(),
                "disbursed_amount": "400000.00",
                "disbursed_on": date.today().isoformat(),
            },
        ),
        "booking",
    )

    # 6 ── The borrower's tracker follows it ─────────────────────────────────────
    j.say("6. App DB: every deal event is applied; the borrower's tracker shows the deal")
    deal_events = _portal_events(j, bid_id, request_id, pipeline_id, since=seen)
    types = sorted({e["type"] for e in deal_events})
    j.say(f"   events for the borrower: {types}")
    assert "deal.progress_updated" in types, f"6: the portal queued no deal progress: {types}"
    for env in deal_events:
        outcome = app.deliver(env)
        assert outcome in ("apply", "duplicate", "stale"), f"6: {env['type']} -> {outcome}"
    refused = [a for a in app.applied if a[1] not in ("apply", "duplicate", "stale")]
    assert not refused, f"6: the borrower app refused: {refused}"
    progress = app.one(
        "SELECT integration_deal_progress_for(CAST(:c AS uuid), CAST(:r AS uuid))",
        {"c": client_id, "r": request_id},
    )
    assert progress, "6: the borrower's tracker is empty after a completed, booked deal"
    j.say(f"   borrower tracker: {json.dumps(progress)[:300]}")
    j.say(f"   App DB applied {len(app.applied)} events: {app.applied}")
    # On CI this becomes a check-run annotation: proof the journey ran (a skip writes nothing).
    print(
        "::notice title=Golden path, both sides::"
        f"request {request_id[:8]} bid {bid_id[:8]} deal {pipeline_id[:8]}; "
        f"App DB applied {len(app.applied)} events "
        f"({', '.join(sorted({t for t, _ in app.applied}))}); "
        f"borrower tracker {'present' if progress else 'EMPTY'}"
    )
