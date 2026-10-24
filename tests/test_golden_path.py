"""
Golden path: one deal, end to end, through the real API.

    borrower publishes a request
      -> bank officer bids -> a second officer approves (four eyes)
      -> the borrower is sent a contract-valid offer
      -> borrower accepts (signed contract call) -> identity revealed to that bank only
      -> deal opens on the bank's own template
      -> every stage completed the way the bank's setup says
         (documents uploaded by the borrower and verified by a second person,
          credit approval through the bank's committee, maker-checker sign-offs)
      -> deal booked in the bank's core system

It runs as MCB, on MCB's live configuration (templates, document checklist,
credit-approval rules, committee), so it answers "would a real deal at MCB go
through today?". Nothing persists:

  * Every request the API makes uses ONE database connection held inside a
    transaction that is always rolled back. Each API session becomes a savepoint;
    the connection's role/JWT claims are re-applied per statement, so tenant
    sessions still run under RLS and service sessions still run privileged.
  * The people acting (officers, checker, credit officer) are created inside
    that transaction. Committee approvers are MCB's real committee members,
    impersonated inside the transaction only.
  * Every outbound HTTP call (webhooks, storage, email, the borrower app) is
    intercepted: nothing leaves the test. Outbox rows are uncommitted, so the
    live dispatcher never sees them.

Run: PORTAL_DB_DSN=... pytest tests/test_golden_path.py -v -s
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import ficium_contract as fc
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from tests.test_integration import PORTAL_DSN, requires_portal

MCB = "f192050a-dfdd-4da7-874e-c3db88f11e41"
B2I_KEY = b"golden-b2i"
ACCEPT_KEY = b"golden-accept"
PDF = b"%PDF-1.4\n% golden path test file\n" + b"0" * 2048

pytestmark = requires_portal


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
class Journey:
    """Shared state + a readable log of each step (printed with -s)."""

    def __init__(self, client: TestClient, conn: Any, outbound: list[dict[str, Any]]) -> None:
        self.c, self.conn, self.outbound = client, conn, outbound
        self.people: dict[str, dict[str, Any]] = {}
        self.webhooks: list[tuple[str, str]] = []

    def say(self, msg: str) -> None:
        print(f"  {msg}", file=sys.stderr)

    def sql(self, q: str, p: dict[str, Any] | None = None) -> Any:
        with Session(bind=self.conn, join_transaction_mode="create_savepoint") as s:
            s.connection().exec_driver_sql("SET LOCAL ROLE NONE")
            r = s.execute(text(q), p or {})
            rows = r.fetchall() if getattr(r, "returns_rows", False) else None
            s.commit()
            return rows

    def as_(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer golden:{who}"}

    def call(self, who: str, method: str, url: str, **kw: Any) -> httpx.Response:
        return self.c.request(method, url, headers=self.as_(who), **kw)

    def signed(self, url: str, body: dict[str, Any], key: bytes, **headers: str) -> httpx.Response:
        raw = json.dumps(body, separators=(",", ":")).encode()
        h = {"Content-Type": "application/json", fc.SIGNATURE_HEADER: fc.sign(raw, key), **headers}
        return self.c.post(url, content=raw, headers=h)


def _person(
    j: Journey, key: str, slug: str, *, member_id: str | None = None, auth_uid: str | None = None
) -> None:
    """A portal user at MCB in the given group (created in-transaction unless ids given)."""
    if member_id is None:
        member_id, auth_uid = str(uuid.uuid4()), str(uuid.uuid4())
        email = f"golden-{key}-{member_id[:6]}@zz.mu"
        j.sql(
            """INSERT INTO auth_portal.auth_users (id, institution_id, email, password_hash)
                 VALUES (CAST(:a AS uuid), CAST(:i AS uuid), :e, 'x')
                 ON CONFLICT (id) DO NOTHING""",
            {"a": auth_uid, "i": MCB, "e": email},
        )
        j.sql(
            """INSERT INTO institution.member (id, institution_id, auth_user_id, email, full_name,
                 is_primary_admin, active, group_id)
                 VALUES (CAST(:m AS uuid), CAST(:i AS uuid), CAST(:a AS uuid), :e, :n, false, true,
                         (SELECT id FROM portal_admin.user_groups WHERE slug = :g))""",
            {"m": member_id, "i": MCB, "a": auth_uid, "e": email, "n": f"Golden {key}", "g": slug},
        )
    else:
        email = j.sql(
            "SELECT email FROM institution.member WHERE id = CAST(:m AS uuid)", {"m": member_id}
        )[0][0]
    perms = j.sql(
        "SELECT module_permissions FROM portal_admin.user_groups WHERE slug = :g", {"g": slug}
    )
    j.people[key] = {
        "sub": auth_uid,
        "member_id": member_id,
        "institution_id": MCB,
        "email": email,
        "user_role": slug,
        "role": "authenticated",
        "module_permissions": list(perms[0][0]) if perms and perms[0][0] else [],
    }


@pytest.fixture()
def journey(monkeypatch: pytest.MonkeyPatch) -> Iterator[Journey]:
    import app.core.db as dbmod
    import app.core.webhooks as webhooks
    import app.main as m
    from app import deps

    engine = create_engine(_sqlalchemy_dsn(PORTAL_DSN or ""), pool_size=1, max_overflow=0)
    conn = engine.connect()
    outer = conn.begin()
    current: dict[str, Any] = {"ctx": None}

    def _apply_ctx(session: Session) -> None:
        ctx = session.info.get("ctx")
        if ctx == current["ctx"]:
            return
        raw = session.connection()
        if ctx is None:  # service session: privileged, no claims
            raw.exec_driver_sql("SET LOCAL ROLE NONE")
            raw.exec_driver_sql("SELECT set_config('request.jwt.claims', '', true)")
        else:
            raw.exec_driver_sql(
                "SELECT set_config('request.jwt.claims', %s, true), "
                "set_config('role', 'authenticated', true)",
                (ctx,),
            )
        current["ctx"] = ctx

    def _session(ctx: str | None) -> Session:
        s = Session(bind=conn, join_transaction_mode="create_savepoint", autoflush=False)
        s.info["ctx"] = ctx
        event.listen(s, "do_orm_execute", lambda st: _apply_ctx(st.session))
        return s

    @contextmanager
    def tenant_session(claims: dict[str, Any]) -> Iterator[Session]:
        s = _session(json.dumps(claims, separators=(",", ":")))
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
            current["ctx"] = "?"  # force re-apply for whoever runs next

    @contextmanager
    def service_session() -> Iterator[Session]:
        s = _session(None)
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
            current["ctx"] = "?"

    for name, mod in list(sys.modules.items()):
        if name == "app" or name.startswith("app."):
            if getattr(mod, "tenant_session", None) is dbmod.tenant_session:
                monkeypatch.setattr(mod, "tenant_session", tenant_session)
            if getattr(mod, "service_session", None) is dbmod.service_session:
                monkeypatch.setattr(mod, "service_session", service_session)
    monkeypatch.setattr(dbmod, "_SessionLocal", lambda: _session(None))

    # Nothing leaves the test.
    outbound: list[dict[str, Any]] = []

    def _fake(request: httpx.Request) -> httpx.Response:
        outbound.append({"method": request.method, "url": str(request.url)})
        if "/storage/v1/object/upload/sign" in str(request.url):
            return httpx.Response(200, json={"url": "/object/upload/sign/x?token=t"})
        if "/storage/v1/object/sign" in str(request.url):
            return httpx.Response(200, json={"signedURL": "/object/sign/x?token=t"})
        return httpx.Response(200, json={"id": "test"})

    real_async = httpx.AsyncClient

    class _NoNetwork(real_async):  # type: ignore[misc,valid-type]
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(_fake)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _NoNetwork)
    webhook_events: list[tuple[str, str]] = []

    async def _dispatch(
        institution_id: str, event_type: str, payload: Any, *a: Any, **k: Any
    ) -> None:
        webhook_events.append((institution_id, event_type))

    monkeypatch.setattr(webhooks, "dispatch_event", _dispatch)

    monkeypatch.setenv("PORTAL_SUPABASE_URL", "https://storage.golden.test")
    monkeypatch.setenv("PORTAL_SUPABASE_SERVICE_KEY", "golden")
    s = m.settings
    monkeypatch.setattr(s, "integration_b2i_verify_keys", B2I_KEY.decode())
    monkeypatch.setattr(s, "integration_acceptance_enabled", True)
    monkeypatch.setattr(s, "integration_acceptance_verify_keys", ACCEPT_KEY.decode())
    monkeypatch.setattr(s, "integration_documents_enabled", True)
    monkeypatch.setattr(s, "integration_public_base_url", "https://golden.test")
    monkeypatch.setattr(s, "integration_i2b_signing_key", "")

    j_holder: dict[str, Journey] = {}

    async def current_claims(authorization: str = deps.Header(default="")) -> dict[str, Any]:
        who = authorization.removeprefix("Bearer golden:")
        return j_holder["j"].people[who]

    m.app.dependency_overrides[deps.current_claims] = current_claims
    client = TestClient(m.app, raise_server_exceptions=False)
    j = Journey(client, conn, outbound)
    j.webhooks = webhook_events
    j_holder["j"] = j
    try:
        yield j
    finally:
        m.app.dependency_overrides.pop(deps.current_claims, None)
        outer.rollback()
        conn.close()
        engine.dispose()


def _sqlalchemy_dsn(dsn: str) -> str:
    return dsn.replace("postgres://", "postgresql+psycopg2://", 1).replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )


def ok(r: httpx.Response, step: str, *codes: int) -> Any:
    assert r.status_code in (codes or (200, 201)), f"{step}: HTTP {r.status_code} {r.text[:600]}"
    return r.json() if r.content else None


# --------------------------------------------------------------------------
# The journey
# --------------------------------------------------------------------------
def test_golden_path(journey: Journey) -> None:
    j = journey
    _person(j, "officer", "bank_officer")
    _person(j, "approver", "bank_officer_approver")
    _person(j, "admin", "institution_admin")
    request_id, anon_id = str(uuid.uuid4()), str(uuid.uuid4())

    # 1 ── Borrower publishes a request (signed contract event) ───────────────
    j.say("1. Borrower publishes a personal loan request")
    env = {
        "id": f"evt_{uuid.uuid4().hex[:24]}",
        "type": "request.published",
        "version": 1,
        "source": "borrower",
        "occurred_at": _now(minutes=-10),
        "aggregate_id": request_id,
        "sequence": 1,
        "data": {
            "request_id": request_id,
            "anon_borrower_id": anon_id,
            "product_type": "personal_loan",
            "amount": 400000,
            "currency": "MUR",
            "term_months": 48,
            "max_rate": None,
            "decision_deadline": _now(days=7),
            "allocation_mode": None,
            "allocations": [],
            "phase1": {
                "loan_purpose": "Home renovation",
                "kyc_verified": True,
                "employment_status": "employed",
                "gross_monthly_income": 85000,
                "dsr_current_pct": 12.5,
                "risk_tier": "B",
                "age": 34,
            },
            "created_at": _now(minutes=-10),
        },
    }
    fc.validate_event(env)
    ok(j.signed("/integration/v1/events", env, B2I_KEY), "publish request", 200, 202)
    status = j.sql(
        "SELECT status FROM marketplace.request WHERE id = CAST(:r AS uuid)", {"r": request_id}
    )
    assert status, "1: the request never reached the marketplace"
    assert status[0][0] == "bidding", f"1: request is '{status[0][0]}', not open for bids"

    # 2 ── The bank sees it, anonymised ──────────────────────────────────────
    j.say("2. Bank officer finds it in the marketplace (no identity)")
    listed = ok(j.call("officer", "GET", "/marketplace/requests"), "marketplace list")
    rows = (
        listed if isinstance(listed, list) else listed.get("items") or listed.get("requests") or []
    )
    mine = [r for r in rows if str(r.get("id")) == request_id]
    assert mine, "2: the officer can't see the new request in the marketplace"
    leaked = {"full_name", "email", "phone", "consumer_id", "employer"} & set(mine[0])
    assert not leaked, f"2: identity fields visible before acceptance: {leaked}"

    # 3 ── Bid, four eyes ────────────────────────────────────────────────────
    j.say("3. Officer bids; it waits for a second person")
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
    action_id = sub["action_id"]
    r = j.call("officer", "POST", f"/approvals/{action_id}/approve", json={})
    assert r.status_code >= 400, "3: the officer approved their own bid (four eyes broken)"
    j.say("   approver signs it off")
    ok(
        j.call("approver", "POST", f"/approvals/{action_id}/approve", json={"note": "ok"}),
        "approve bid",
    )
    bid = j.sql(
        """SELECT id, status, rate FROM marketplace.bid
                   WHERE request_id = CAST(:r AS uuid) AND institution_id = CAST(:i AS uuid)""",
        {"r": request_id, "i": MCB},
    )
    assert bid and bid[0][1] in ("submitted", "under_review"), (
        f"3: no live bid after approval: {bid}"
    )
    bid_id = str(bid[0][0])

    # 4 ── Borrower is told, in contract form ────────────────────────────────
    j.say("4. Borrower side receives a contract-valid offer")
    out = j.sql(
        """SELECT envelope FROM integration.outbox
                   WHERE aggregate_id = :a AND type = 'bid.placed' ORDER BY sequence""",
        {"a": bid_id},
    )
    if not out:
        out = j.sql(
            """SELECT envelope FROM integration.outbox
                       WHERE type = 'bid.placed' AND envelope->'data'->>'request_id' = :r""",
            {"r": request_id},
        )
    assert out, "4: no bid.placed event queued for the borrower app"
    offer = out[-1][0]
    fc.validate_event(offer)
    assert "MCB" not in json.dumps(offer["data"].get("phase1", {})), offer

    # 5 ── Acceptance: the one synchronous call ──────────────────────────────
    j.say("5. Borrower accepts; identity released to MCB only")
    accept = {
        "bid_id": bid_id,
        "request_id": request_id,
        "anon_borrower_id": anon_id,
        "consent_ref": "consent_golden",
        "released_identity": {
            "full_name": "Golden Path Borrower",
            "email": "golden.borrower@example.com",
            "phone": "+230 5555 0000",
        },
    }
    fc.validate_acceptance_request(accept)
    idem = f"golden-{uuid.uuid4()}"
    first = ok(
        j.signed("/integration/v1/acceptances", accept, ACCEPT_KEY, **{"Idempotency-Key": idem}),
        "accept",
    )
    fc.validate_acceptance_response(first)
    pipeline_id = first["pipeline_id"]
    assert first["institution"]["institution_id"] == MCB
    again = ok(
        j.signed("/integration/v1/acceptances", accept, ACCEPT_KEY, **{"Idempotency-Key": idem}),
        "accept replay",
    )
    assert again == first, "5: a retried acceptance gave a different answer"

    # 6 ── Reveal to the winning bank ────────────────────────────────────────
    j.say("6. MCB sees who the borrower is")
    rev = ok(j.call("officer", "GET", f"/marketplace/bids/{bid_id}/reveal"), "reveal")
    assert "Golden Path Borrower" in json.dumps(rev), (
        f"6: identity not revealed to the winner: {rev}"
    )

    # 7 ── Work the deal through MCB's own stages ────────────────────────────
    j.say("7. Deal opens on MCB's template; working every stage")
    for _ in range(30):
        deal = ok(j.call("officer", "GET", f"/pipelines/{pipeline_id}"), "open deal")
        if deal.get("status") != "active":
            break
        stage = _active_stage(deal)
        assert stage, f"7: deal is active but no stage is active: {deal.get('stages')}"
        sid, label = str(stage["id"]), stage.get("label") or stage.get("stage_label")
        j.say(f"   stage {stage.get('position')}: {label} [{stage.get('status')}]")
        _satisfy_documents(j, pipeline_id, sid, anon_id)
        if stage.get("status") == "awaiting_approval":
            _approve_stage(j, pipeline_id, sid, stage)
            continue
        r = j.call(
            "officer",
            "POST",
            f"/pipelines/{pipeline_id}/stages/{sid}/advance",
            json={"notes": "done"},
        )
        if r.status_code == 409 and "credit system" in r.text:
            ok(
                j.call(
                    "admin",
                    "POST",
                    f"/pipelines/{pipeline_id}/credit-decision",
                    json={"outcome": "approved", "external_reference": "GOLDEN-CR-1"},
                ),
                f"credit decision: {label}",
            )
            continue
        res = ok(r, f"advance '{label}'")
        if res.get("status") in ("awaiting_approval", "pending_approval") or res.get(
            "approval_instance_id"
        ):
            _approve_stage(j, pipeline_id, sid, stage)
    else:
        raise AssertionError("7: the deal never finished (30 steps)")
    assert deal["status"] == "completed", (
        f"7: deal ended '{deal['status']}': {deal.get('closed_reason')}"
    )

    # 8 ── Booked in the core system ─────────────────────────────────────────
    j.say("8. Officer records the booking from the core banking system")
    booked = ok(
        j.call(
            "officer",
            "POST",
            f"/pipelines/{pipeline_id}/booking",
            json={
                "core_reference": f"GOLDEN-{uuid.uuid4().hex[:8]}",
                "booked_on": date.today().isoformat(),
                "disbursed_amount": "400000.00",
                "disbursed_on": date.today().isoformat(),
            },
        ),
        "booking",
    )
    assert booked, "8: booking returned nothing"

    # 9 ── Borrower kept informed ────────────────────────────────────────────
    j.say("9. Borrower side received the deal's progress")
    types = [
        r[0]
        for r in j.sql(
            """SELECT DISTINCT type FROM integration.outbox
                                    WHERE envelope::text LIKE :p""",
            {"p": f"%{request_id}%"},
        )
    ]
    j.say(f"   events for the borrower: {sorted(types)}")
    for (envelope,) in j.sql(
        "SELECT envelope FROM integration.outbox WHERE envelope::text LIKE :p",
        {"p": f"%{request_id}%"},
    ):
        fc.validate_event(envelope)
    j.say(
        f"   outbound HTTP calls intercepted: {len(j.outbound)}, webhooks recorded: "
        f"{len(j.webhooks)} (none left the test)"
    )


# --------------------------------------------------------------------------
# Stage helpers
# --------------------------------------------------------------------------
def _active_stage(deal: dict[str, Any]) -> dict[str, Any] | None:
    for s in deal.get("stages") or []:
        if s.get("status") in ("active", "awaiting_approval"):
            return s
    return None


def _satisfy_documents(j: Journey, pipeline_id: str, stage_id: str, anon_id: str) -> None:
    """Every required document for this stage: provided (by the borrower through the
    contract, or by the bank), then verified by a second person."""
    docs = ok(j.call("officer", "GET", f"/pipelines/{pipeline_id}/documents"), "document checklist")
    stage_def = j.sql(
        "SELECT stage_def_id FROM marketplace.pipeline_stage_instance WHERE id = CAST(:s AS uuid)",
        {"s": stage_id},
    )[0][0]
    for d in docs:
        if str(d.get("stage_def_id")) != str(stage_def) or not d.get("mandatory"):
            continue
        if d["status"] in ("verified", "waived"):
            continue
        name = d.get("name")
        if d["status"] in ("required", "rejected"):
            if d.get("provided_by") == "borrower":
                j.say(f"     borrower uploads '{name}' through the contract")
                _borrower_upload(j, pipeline_id, anon_id, str(d["requirement_id"]))
            else:
                j.say(f"     bank uploads '{name}'")
                up = ok(
                    j.call(
                        "officer",
                        "POST",
                        f"/pipelines/{pipeline_id}/documents/{d['id']}/upload-url",
                        json={
                            "file_name": "golden.pdf",
                            "mime_type": "application/pdf",
                            "file_size": len(PDF),
                        },
                    ),
                    f"upload url: {name}",
                )
                ok(
                    j.call(
                        "officer",
                        "POST",
                        f"/pipelines/{pipeline_id}/documents/{d['id']}/receive",
                        json={
                            "storage_path": up.get("storage_path") or up.get("path"),
                            "file_name": "golden.pdf",
                            "mime_type": "application/pdf",
                            "file_size": len(PDF),
                        },
                    ),
                    f"receive: {name}",
                )
        j.say(f"     approver verifies '{name}'")
        ok(
            j.call(
                "approver", "POST", f"/pipelines/{pipeline_id}/documents/{d['id']}/verify", json={}
            ),
            f"verify: {name}",
        )


def _borrower_upload(j: Journey, pipeline_id: str, anon_id: str, requirement_id: str) -> None:
    slot = {
        "pipeline_id": pipeline_id,
        "anon_borrower_id": anon_id,
        "requirement_id": requirement_id,
        "file_name": "golden.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(PDF),
        "source": "new",
    }
    fc.validate_document_upload_request(slot)
    up = ok(
        j.signed("/integration/v1/deal-documents/uploads", slot, ACCEPT_KEY), "borrower upload slot"
    )
    path = "/" + up["upload_url"].split("://", 1)[1].split("/", 1)[1]
    r = j.c.put(
        path,
        content=PDF,
        headers={"Content-Type": "application/pdf", fc.SIGNATURE_HEADER: fc.sign(PDF, ACCEPT_KEY)},
    )
    ok(r, "borrower upload content")
    done = {
        "upload_id": up["upload_id"],
        "pipeline_id": pipeline_id,
        "anon_borrower_id": anon_id,
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }
    ok(
        j.signed(
            f"/integration/v1/deal-documents/uploads/{up['upload_id']}/complete", done, ACCEPT_KEY
        ),
        "borrower upload complete",
    )


def _approve_stage(j: Journey, pipeline_id: str, stage_id: str, stage: dict[str, Any]) -> None:
    inst = j.sql(
        """SELECT approval_instance_id FROM marketplace.pipeline_stage_instance
                    WHERE id = CAST(:s AS uuid)""",
        {"s": stage_id},
    )[0][0]
    if inst is None:
        j.say("     checker signs off the stage")
        ok(
            j.call(
                "approver", "POST", f"/pipelines/{pipeline_id}/stages/{stage_id}/approve", json={}
            ),
            "stage approve",
        )
        return
    for _ in range(10):
        pending = j.sql(
            """
            SELECT asi.id, m.id AS member_id, m.auth_user_id, g.slug, sd.name
              FROM institution.approval_stage_instance asi
              JOIN institution.approval_instance ai ON ai.id = asi.instance_id
              JOIN institution.approval_stage_def sd ON sd.id = asi.stage_def_id
              JOIN institution.member m ON m.institution_id = ai.institution_id AND m.active
              LEFT JOIN portal_admin.user_groups g ON g.id = m.group_id
             WHERE asi.instance_id = CAST(:i AS uuid) AND asi.status = 'active'
               AND m.auth_user_id::text IS DISTINCT FROM ai.entity_maker_id::text
               AND m.id::text IS DISTINCT FROM ai.entity_maker_id::text
               AND (   (sd.committee_id IS NOT NULL AND EXISTS (
                          SELECT 1 FROM institution.committee_member cm
                           WHERE cm.committee_id = sd.committee_id AND cm.member_id = m.id
                             AND cm.is_voting AND (cm.valid_to IS NULL OR cm.valid_to > now())))
                    OR (sd.committee_id IS NULL
                        AND sd.approver_role IN (g.slug, m.role, m.member_role)))
               AND NOT EXISTS (SELECT 1 FROM institution.approval_action a
                                WHERE a.stage_instance_id = asi.id
                                  AND a.actor_id::text IN (m.id::text, m.auth_user_id::text))
             ORDER BY asi.seq, (m.auth_user_id::text LIKE '%') DESC LIMIT 1""",
            {"i": str(inst)},
        )
        if not pending:
            st = j.sql(
                "SELECT status FROM institution.approval_instance WHERE id = CAST(:i AS uuid)",
                {"i": str(inst)},
            )[0][0]
            if st == "approved":
                return
            raise AssertionError(f"7: credit approval is '{st}' and nobody at MCB can act on it")
        asi, member_id, auth_uid, slug, step = pending[0]
        key = f"approver-{str(member_id)[:6]}"
        if key not in j.people:
            _person(
                j,
                key,
                slug or "institution_admin",
                member_id=str(member_id),
                auth_uid=str(auth_uid),
            )
        j.say(f"     credit approval '{step}': {key} approves")
        ok(
            j.call(
                key,
                "POST",
                f"/approval-engine/instances/{inst}/actions",
                json={"stage_instance_id": str(asi), "action": "approve", "comment": "golden path"},
            ),
            "credit approval vote",
        )
    raise AssertionError("7: credit approval never completed")


def _now(days: int = 0, minutes: int = 0) -> str:
    # Inside one transaction now() is frozen at its start, so "just now" in the
    # past keeps the request inside its bid window.
    return (datetime.now(UTC) + timedelta(days=days, minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
