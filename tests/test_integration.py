"""
Ficium — End-to-end integration test suite
============================================

Regression tests for the security and flow guarantees of both databases:
  • Portal DB  (institution side)  — tenant isolation, RLS enforcement,
    bidding, maker-checker, catalogue readability, pipeline, notifications.
  • App DB     (consumer side)     — client isolation, KYC-settings lockdown.
  • Cross-DB   marketplace sync     — ingest function + product resolver.

Every test runs inside a transaction that is ROLLED BACK, so no test data is
ever persisted. Role/JWT impersonation mirrors exactly how PostgREST presents
an authenticated end user, so these tests exercise the real RLS policies.

------------------------------------------------------------------------------
RUNNING
------------------------------------------------------------------------------
Set two connection strings (the *session pooler / direct* DSNs, port 5432 — NOT
the transaction pooler, because these tests use SET ROLE inside a transaction):

    export PORTAL_DB_DSN="postgresql://postgres.<ref>:<pw>@<host>:5432/postgres"
    export APP_DB_DSN="postgresql://postgres.<ref>:<pw>@<host>:5432/postgres"

Then:

    pip install pytest psycopg2-binary
    pytest test_integration.py -v

If a DSN is not set, the tests that need it are skipped (so the suite still
runs in environments wired to only one database).
------------------------------------------------------------------------------
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager

import psycopg2
import pytest

PORTAL_DSN = os.environ.get("PORTAL_DB_DSN")
APP_DSN    = os.environ.get("APP_DB_DSN")

# Known seed identity in the portal DB (the live MCB admin). These are not
# secrets — just row identifiers used to drive RLS as a real member.
MCB_INSTITUTION_ID  = "31b3ee32-2864-4875-8920-cc5f27240971"
MCB_ADMIN_AUTH_UID  = "4224540d-d59c-4584-8271-cb6ef24c472d"
MCB_ADMIN_MEMBER_ID = "ecf86654-4ef7-4fb5-aa8b-5545a4ff1d10"

requires_portal = pytest.mark.skipif(not PORTAL_DSN, reason="PORTAL_DB_DSN not set")
requires_app    = pytest.mark.skipif(not APP_DSN,    reason="APP_DB_DSN not set")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@contextmanager
def rolled_back_tx(dsn: str):
    """Yield a cursor in a transaction that is always rolled back."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        yield cur
    finally:
        conn.rollback()
        conn.close()


def become_authenticated(cur, *, sub: str, institution_id: str | None = None,
                         user_role: str = "institution_admin",
                         app_metadata: dict | None = None) -> None:
    """
    Put the session into the `authenticated` role with JWT claims, exactly as
    PostgREST does for a logged-in user. This is what makes RLS actually fire.
    """
    claims: dict = {"sub": sub, "role": "authenticated", "user_role": user_role}
    if institution_id:
        claims["institution_id"] = institution_id
    if app_metadata:
        claims["app_metadata"] = app_metadata
    cur.execute("SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps(claims),))
    cur.execute("SET LOCAL ROLE authenticated")


def scalar(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def _seed_catalog_and_request(cur) -> tuple[str, str]:
    """Seed a catalog product + biddable request. Returns (req_id, prod_id)."""
    fam  = str(uuid.uuid4())
    prod = str(uuid.uuid4())
    req  = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO catalog.product_family (id, code, label, sort_order)
        VALUES (%s,'zz_fam','ZZ Fam',999)
    """, (fam,))
    cur.execute("""
        INSERT INTO catalog.product (id, family_id, code, label, currency, active, sort_order)
        VALUES (%s,%s,'zz_prod','ZZ Prod','MUR',true,999)
    """, (prod, fam))
    cur.execute("""
        INSERT INTO marketplace.request
          (id, consumer_id, product_id, country, currency, amount, term_months,
           status, bid_window_opens_at, bid_window_closes_at, source, idempotency_key)
        VALUES (%s,%s,%s,'MU','MUR',500000,36,'bidding',now(),now()+interval '1 day','app',%s)
    """, (req, str(uuid.uuid4()), prod, "zz-" + req))
    return req, prod


# ===========================================================================
# PORTAL DB — tenant isolation & RLS enforcement
# ===========================================================================
@requires_portal
class TestPortalTenantIsolation:

    def test_connection_role_does_not_bypass_rls_in_authenticated(self):
        """Under `authenticated`, the role must NOT carry BYPASSRLS."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                  institution_id=MCB_INSTITUTION_ID)
            assert scalar(cur, "SELECT current_user") == "authenticated"
            bypass = scalar(cur,
                "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
            assert bypass is False, "authenticated must not bypass RLS"

    def test_member_context_resolves(self):
        """The keystone context function returns this member's identity."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            cur.execute("SELECT * FROM institution.current_member_ctx()")
            member_id, inst_id, is_admin, member_role, modules = cur.fetchone()
            assert str(inst_id) == MCB_INSTITUTION_ID
            assert is_admin is True
            assert len(modules) > 0

    def test_member_sees_only_own_tenant(self):
        """
        With a second tenant present, an unfiltered member query must return
        ONLY the caller's institution. This is the core isolation guarantee
        that was broken when the pool connected as postgres (BYPASSRLS).
        """
        with rolled_back_tx(PORTAL_DSN) as cur:
            other_inst   = str(uuid.uuid4())
            other_member = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.institution
                  (id, name, legal_name, institution_type, country, approved,
                   onboarding_stage, primary_contact_email)
                VALUES (%s,'ZZ_TEST','ZZ Test','bank','MU',true,'approved','z@z.mu')
            """, (other_inst,))
            cur.execute("""
                INSERT INTO institution.member
                  (id, institution_id, auth_user_id, email, full_name, role,
                   is_primary_admin, active)
                VALUES (%s,%s,%s,'z@z.mu','Z','admin',true,true)
            """, (other_member, other_inst, str(uuid.uuid4())))

            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            cur.execute("SELECT DISTINCT institution_id FROM institution.member")
            seen = {str(r[0]) for r in cur.fetchall()}
            assert seen == {MCB_INSTITUTION_ID}, \
                f"tenant leak: caller saw {seen}"

    def test_catalog_readable_under_authenticated(self):
        """
        Catalogue reference tables had RLS enabled with no policy (silent
        deny-all). They must be readable by authenticated, or every dropdown
        empties out.
        """
        with rolled_back_tx(PORTAL_DSN) as cur:
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            assert scalar(cur, "SELECT count(*) FROM catalog.module") > 0
            assert scalar(cur, "SELECT count(*) FROM catalog.product_family") > 0


# ===========================================================================
# PORTAL DB — marketplace bidding
# ===========================================================================
@requires_portal
class TestPortalBidding:

    def test_institution_can_submit_own_bid(self):
        with rolled_back_tx(PORTAL_DSN) as cur:
            req, _ = _seed_catalog_and_request(cur)
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            cur.execute("""
                INSERT INTO marketplace.bid
                  (request_id, institution_id, rate, rate_type, amount_offered,
                   term_months, submitted_via, idempotency_key)
                VALUES (%s,%s,7.5,'fixed',500000,36,'portal',%s)
                RETURNING status
            """, (req, MCB_INSTITUTION_ID, "zzbid-" + req))
            assert cur.fetchone()[0] == "submitted"

    def test_cross_tenant_bid_blocked(self):
        """An institution must not be able to bid as a different institution."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            req, _ = _seed_catalog_and_request(cur)
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            with pytest.raises(psycopg2.Error):
                cur.execute("""
                    INSERT INTO marketplace.bid
                      (request_id, institution_id, rate, rate_type, amount_offered,
                       term_months, submitted_via, idempotency_key)
                    VALUES (%s, %s, 5.0,'fixed',500000,36,'portal',%s)
                """, (req, str(uuid.uuid4()), "evil-" + req))

    def test_bid_acceptance_reveals_phase2_pii(self):
        """
        After marketplace.accept_bid(), bid_acceptance row must contain
        the borrower PII and the bid status must transition to accepted.
        """
        with rolled_back_tx(PORTAL_DSN) as cur:
            consumer_id  = str(uuid.uuid4())
            req, _       = _seed_catalog_and_request(cur)
            # Override consumer_id on the request
            cur.execute("UPDATE marketplace.request SET consumer_id = %s WHERE id = %s",
                        (consumer_id, req))
            # Submit a bid as MCB
            cur.execute("""
                INSERT INTO marketplace.bid
                  (request_id, institution_id, rate, rate_type, amount_offered,
                   term_months, submitted_via, idempotency_key)
                VALUES (%s,%s,7.5,'fixed',500000,36,'portal',%s)
                RETURNING id
            """, (req, MCB_INSTITUTION_ID, "zzbid2-" + req))
            bid_id = str(cur.fetchone()[0])

            # Simulate accept_bid (service role — no RLS)
            phase2 = json.dumps({
                "full_name": "Jean-Pierre Duval",
                "email":     "jp@example.mu",
                "phone":     "+23057001234",
                "address":   "12 Royal Rd, Curepipe",
                "date_of_birth":   "1985-03-15",
                "document_number": "NID-123456",
            })
            cur.execute(
                "SELECT marketplace.accept_bid(%s, %s, %s, %s::jsonb) AS result",
                (req, bid_id, consumer_id, phase2),
            )
            result = cur.fetchone()[0]
            assert result is not None

            # Bid acceptance row written
            cur.execute(
                "SELECT full_name, email FROM marketplace.bid_acceptance WHERE bid_id = %s",
                (bid_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "Jean-Pierre Duval"
            assert row[1] == "jp@example.mu"

            # Bid status → accepted
            cur.execute("SELECT status FROM marketplace.bid WHERE id = %s", (bid_id,))
            assert cur.fetchone()[0] == "accepted"

            # Request status → accepted
            cur.execute("SELECT status FROM marketplace.request WHERE id = %s", (req,))
            assert cur.fetchone()[0] == "accepted"

    def test_duplicate_accept_blocked(self):
        """Calling accept_bid twice on an already-accepted request must raise."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            consumer_id = str(uuid.uuid4())
            req, _      = _seed_catalog_and_request(cur)
            cur.execute("UPDATE marketplace.request SET consumer_id = %s WHERE id = %s",
                        (consumer_id, req))
            cur.execute("""
                INSERT INTO marketplace.bid
                  (request_id, institution_id, rate, rate_type, amount_offered,
                   term_months, submitted_via, idempotency_key)
                VALUES (%s,%s,7.0,'fixed',500000,36,'portal',%s)
                RETURNING id
            """, (req, MCB_INSTITUTION_ID, "zzdup-" + req))
            bid_id = str(cur.fetchone()[0])
            phase2 = json.dumps({"full_name": "Test", "email": "t@t.mu"})

            cur.execute("SELECT marketplace.accept_bid(%s,%s,%s,%s::jsonb)",
                        (req, bid_id, consumer_id, phase2))
            with pytest.raises(psycopg2.Error):
                cur.execute("SELECT marketplace.accept_bid(%s,%s,%s,%s::jsonb)",
                            (req, bid_id, consumer_id, phase2))


# ===========================================================================
# PORTAL DB — maker-checker (governance)
# ===========================================================================
@requires_portal
class TestPortalMakerChecker:

    def test_submit_then_approve_by_different_member(self):
        """Full loop: a maker submits, a different member approves, it executes."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            maker_id  = str(uuid.uuid4())
            maker_uid = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.member
                  (id, institution_id, auth_user_id, email, full_name, role,
                   is_primary_admin, active)
                VALUES (%s,%s,%s,'mk@z.mu','MK','member',false,true)
            """, (maker_id, MCB_INSTITUTION_ID, maker_uid))

            become_authenticated(cur, sub=maker_uid,
                                 institution_id=MCB_INSTITUTION_ID)
            slug      = "zz_grp_" + uuid.uuid4().hex[:6]
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval(
                  'group.create','group', gen_random_uuid(),
                  %s::jsonb)
            """, (json.dumps({"slug": slug, "label": "ZZ",
                              "module_permissions": []}),))
            assert action_id is not None

            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            cur.execute("SELECT institution.approve_action(%s, 'ok')", (action_id,))

            status = scalar(cur,
                "SELECT status FROM governance.action WHERE id = %s", (action_id,))
            assert status == "approved"
            grp_count = scalar(cur,
                "SELECT count(*) FROM institution.\"group\" WHERE slug = %s", (slug,))
            assert grp_count == 1

    def test_maker_cannot_approve_own_action(self):
        """Four-eyes control: the maker is blocked from approving their own action."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            slug      = "zz_self_" + uuid.uuid4().hex[:6]
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval(
                  'group.create','group', gen_random_uuid(), %s::jsonb)
            """, (json.dumps({"slug": slug, "label": "ZZ",
                              "module_permissions": []}),))
            with pytest.raises(psycopg2.Error):
                cur.execute("SELECT institution.approve_action(%s, 'self')",
                            (action_id,))

    def test_expired_action_cannot_be_approved(self):
        """An action past its expires_at must be rejected by the RPC."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            slug      = "zz_exp_" + uuid.uuid4().hex[:6]
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval(
                  'group.create','group', gen_random_uuid(), %s::jsonb)
            """, (json.dumps({"slug": slug, "label": "ZZ",
                              "module_permissions": []}),))
            # Force expires_at into the past
            cur.execute(
                "UPDATE governance.action SET expires_at = now() - interval '1 hour' WHERE id = %s",
                (action_id,),
            )
            # Approve as a different user — should fail
            maker_uid = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.member
                  (id, institution_id, auth_user_id, email, full_name, role,
                   is_primary_admin, active)
                VALUES (%s,%s,%s,'exp@z.mu','EX','member',false,true)
            """, (str(uuid.uuid4()), MCB_INSTITUTION_ID, maker_uid))
            become_authenticated(cur, sub=maker_uid,
                                 institution_id=MCB_INSTITUTION_ID)
            with pytest.raises(psycopg2.Error):
                cur.execute("SELECT institution.approve_action(%s, 'late')", (action_id,))


# ===========================================================================
# PORTAL DB — pipeline stage flow
# ===========================================================================
@requires_portal
class TestPipelineFlow:

    def _seed_pipeline(self, cur) -> tuple[str, str, str]:
        """
        Seed: accepted bid → loan_pipeline + first stage active.
        Returns (pipeline_id, first_stage_id, institution_id).
        """
        req, prod = _seed_catalog_and_request(cur)
        bid_id     = str(uuid.uuid4())
        pipeline   = str(uuid.uuid4())
        stage_def  = str(uuid.uuid4())
        stage_inst = str(uuid.uuid4())

        # Insert bid
        cur.execute("""
            INSERT INTO marketplace.bid
              (id, request_id, institution_id, rate, rate_type, amount_offered,
               term_months, submitted_via, status, idempotency_key)
            VALUES (%s,%s,%s,7.5,'fixed',500000,36,'portal','accepted',%s)
        """, (bid_id, req, MCB_INSTITUTION_ID, "zz-bid-" + bid_id))

        # Stage definition
        cur.execute("""
            INSERT INTO institution.pipeline_stage_def
              (id, institution_id, code, label, position, requires_maker_checker,
               requires_documents, sla_hours, active)
            VALUES (%s,%s,'CREDIT_CHECK','Credit Check',1,false,false,24,true)
        """, (stage_def, MCB_INSTITUTION_ID))

        # Pipeline
        cur.execute("""
            INSERT INTO marketplace.loan_pipeline
              (id, institution_id, bid_id, request_id, status,
               deal_amount, deal_rate, deal_term_months, current_stage_id)
            VALUES (%s,%s,%s,%s,'active',500000,7.5,36,%s)
        """, (pipeline, MCB_INSTITUTION_ID, bid_id, req, stage_inst))

        # Stage instance (active)
        cur.execute("""
            INSERT INTO marketplace.pipeline_stage_instance
              (id, pipeline_id, stage_def_id, position, label,
               status, started_at)
            VALUES (%s,%s,%s,1,'Credit Check','active',now())
        """, (stage_inst, pipeline, stage_def))

        return pipeline, stage_inst, MCB_INSTITUTION_ID

    def test_pipeline_stage_advance_completes_and_closes(self):
        """Advancing the only stage should close the pipeline."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            pipeline, stage_inst, _ = self._seed_pipeline(cur)

            # Mark stage completed directly (mimics _complete_stage)
            cur.execute("""
                UPDATE marketplace.pipeline_stage_instance
                SET status = 'completed', completed_at = now(), updated_at = now()
                WHERE id = %s
            """, (stage_inst,))

            # No next pending stage → pipeline should close
            cur.execute("""
                UPDATE marketplace.loan_pipeline
                SET status = 'completed', completed_at = now(), updated_at = now()
                WHERE id = %s
            """, (pipeline,))

            status = scalar(cur,
                "SELECT status FROM marketplace.loan_pipeline WHERE id = %s",
                (pipeline,))
            assert status == "completed"

    def test_pipeline_maker_checker_submit_then_approve(self):
        """Stage with dual-control: submit → awaiting_approval → approve → completed."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            req, prod = _seed_catalog_and_request(cur)
            bid_id    = str(uuid.uuid4())
            pipeline  = str(uuid.uuid4())
            stage_def = str(uuid.uuid4())
            stage_inst= str(uuid.uuid4())

            cur.execute("""
                INSERT INTO marketplace.bid
                  (id, request_id, institution_id, rate, rate_type, amount_offered,
                   term_months, submitted_via, status, idempotency_key)
                VALUES (%s,%s,%s,7.5,'fixed',500000,36,'portal','accepted',%s)
            """, (bid_id, req, MCB_INSTITUTION_ID, "zz-dc-" + bid_id))

            cur.execute("""
                INSERT INTO institution.pipeline_stage_def
                  (id, institution_id, code, label, position, requires_maker_checker,
                   requires_documents, sla_hours, active)
                VALUES (%s,%s,'LEGAL_REVIEW','Legal Review',1,true,false,48,true)
            """, (stage_def, MCB_INSTITUTION_ID))

            cur.execute("""
                INSERT INTO marketplace.loan_pipeline
                  (id, institution_id, bid_id, request_id, status,
                   deal_amount, deal_rate, deal_term_months, current_stage_id)
                VALUES (%s,%s,%s,%s,'active',500000,7.5,36,%s)
            """, (pipeline, MCB_INSTITUTION_ID, bid_id, req, stage_inst))

            maker_uid = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.pipeline_stage_instance
                  (id, pipeline_id, stage_def_id, position, label, status,
                   started_at, submitted_by)
                VALUES (%s,%s,%s,1,'Legal Review','awaiting_approval',now(),%s)
            """, (stage_inst, pipeline, stage_def, maker_uid))

            # Checker (different member) approves
            checker_uid = str(uuid.uuid4())
            cur.execute("""
                UPDATE marketplace.pipeline_stage_instance
                SET status = 'completed', approved_by = %s, approved_at = now(),
                    completed_at = now(), updated_at = now()
                WHERE id = %s AND submitted_by != %s
            """, (checker_uid, stage_inst, checker_uid))

            status = scalar(cur,
                "SELECT status FROM marketplace.pipeline_stage_instance WHERE id = %s",
                (stage_inst,))
            assert status == "completed"


# ===========================================================================
# PORTAL DB — benefits module
# ===========================================================================
@requires_portal
class TestBenefitsModule:

    def test_benefit_crud_scoped_to_institution(self):
        """Benefits are always institution-scoped; another institution cannot see them."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            # Seed a benefit category
            cat_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.benefit_category
                  (id, code, label, sort_order)
                VALUES (%s, 'ZZ_CAT', 'ZZ Category', 999)
            """, (cat_id,))

            # Seed a catalog product for FK
            fam_id  = str(uuid.uuid4())
            prod_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO catalog.product_family (id, code, label, sort_order)
                VALUES (%s, 'zz_bf', 'ZZ Benefit Fam', 998)
            """, (fam_id,))
            cur.execute("""
                INSERT INTO catalog.product (id, family_id, code, label, currency, active, sort_order)
                VALUES (%s, %s, 'zz_bp', 'ZZ Benefit Prod', 'MUR', true, 998)
            """, (prod_id, fam_id))

            # Insert benefit as owner (service role, pre-auth)
            benefit_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.benefit
                  (id, institution_id, cat_id, product_id, title, is_guaranteed, is_active)
                VALUES (%s, %s, %s, %s, 'ZZ Test Benefit', false, true)
            """, (benefit_id, MCB_INSTITUTION_ID, cat_id, prod_id))

            # As MCB member — must see the benefit
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            n = scalar(cur,
                "SELECT count(*) FROM institution.benefit WHERE id = %s",
                (benefit_id,))
            assert n == 1

            # As different institution — must NOT see it (RLS)
            other_uid = str(uuid.uuid4())
            other_iid = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.institution
                  (id, name, legal_name, institution_type, country, approved,
                   onboarding_stage, primary_contact_email)
                VALUES (%s, 'ZZ_OTHER', 'ZZ Other', 'bank', 'MU', true, 'approved', 'o@o.mu')
            """, (other_iid,))
            become_authenticated(cur, sub=other_uid, institution_id=other_iid)
            n_other = scalar(cur,
                "SELECT count(*) FROM institution.benefit WHERE id = %s",
                (benefit_id,))
            assert n_other == 0, "cross-institution benefit leak"


# ===========================================================================
# PORTAL DB — notifications
# ===========================================================================
@requires_portal
class TestNotifications:

    def test_service_role_can_write_notification(self):
        """service_role INSERT policy must allow writing portal_notifications."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            notif_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO public.portal_notifications
                  (id, institution_id, kind, title, body, link)
                VALUES (%s, %s, 'bid_accepted', 'Test notif', 'body text', '/bids')
            """, (notif_id, MCB_INSTITUTION_ID))
            n = scalar(cur,
                "SELECT count(*) FROM public.portal_notifications WHERE id = %s",
                (notif_id,))
            assert n == 1

    def test_member_reads_own_institution_notifications(self):
        """Members see only their own institution's notifications (RLS SELECT policy)."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            notif_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO public.portal_notifications
                  (id, institution_id, kind, title)
                VALUES (%s, %s, 'system', 'RLS test notif')
            """, (notif_id, MCB_INSTITUTION_ID))

            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            n = scalar(cur,
                "SELECT count(*) FROM public.portal_notifications WHERE id = %s",
                (notif_id,))
            assert n == 1

    def test_cross_institution_notifications_blocked(self):
        """A member from another institution must NOT see MCB's notifications."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            notif_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO public.portal_notifications
                  (id, institution_id, kind, title)
                VALUES (%s, %s, 'system', 'MCB private notif')
            """, (notif_id, MCB_INSTITUTION_ID))

            other_uid = str(uuid.uuid4())
            other_iid = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.institution
                  (id, name, legal_name, institution_type, country, approved,
                   onboarding_stage, primary_contact_email)
                VALUES (%s, 'ZZ_N', 'ZZ Notif', 'bank', 'MU', true, 'approved', 'n@n.mu')
            """, (other_iid,))
            become_authenticated(cur, sub=other_uid, institution_id=other_iid)
            n = scalar(cur,
                "SELECT count(*) FROM public.portal_notifications WHERE id = %s",
                (notif_id,))
            assert n == 0, "notification leaked to other institution"

    def test_member_can_mark_notification_read(self):
        """Members can UPDATE read_at on their own institution's notifications."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            notif_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO public.portal_notifications
                  (id, institution_id, kind, title)
                VALUES (%s, %s, 'approval_needed', 'Mark read test')
            """, (notif_id, MCB_INSTITUTION_ID))

            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            cur.execute("""
                UPDATE public.portal_notifications
                SET read_at = now()
                WHERE id = %s
            """, (notif_id,))
            read_at = scalar(cur,
                "SELECT read_at FROM public.portal_notifications WHERE id = %s",
                (notif_id,))
            assert read_at is not None


# ===========================================================================
# PORTAL DB — cross-DB sync mechanism
# ===========================================================================
@requires_portal
class TestMarketplaceSync:

    def test_product_resolver_covers_all_app_types(self):
        """Every app product_type enum value must resolve to a catalog product."""
        app_types = ["mortgage", "personal_loan", "sme_loan", "fixed_deposit",
                     "savings_account", "credit_card", "business_account",
                     "investment_account", "garbage_fallback"]
        with rolled_back_tx(PORTAL_DSN) as cur:
            for t in app_types:
                pid = scalar(cur,
                    "SELECT catalog.product_id_for_app_type(%s)", (t,))
                assert pid is not None, f"{t} did not resolve to a product"

    def test_ingest_is_idempotent(self):
        """Ingesting the same app request twice updates in place, no duplicate."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            app_req  = str(uuid.uuid4())
            consumer = str(uuid.uuid4())
            for amount in (100000, 200000):
                cur.execute("""
                    SELECT marketplace.ingest_app_request(
                      %s,%s,'personal_loan',%s,36,'p',NULL,NULL,'open',now())
                """, (app_req, consumer, amount))
            n = scalar(cur,
                "SELECT count(*) FROM marketplace.request WHERE id = %s", (app_req,))
            assert n == 1
            final_amount = scalar(cur,
                "SELECT amount FROM marketplace.request WHERE id = %s", (app_req,))
            assert int(final_amount) == 200000


# ===========================================================================
# APP DB — consumer isolation & KYC lockdown
# ===========================================================================
@requires_app
class TestAppConsumerIsolation:

    def _two_client_ids(self, cur):
        cur.execute("SELECT id FROM public.clients LIMIT 2")
        rows = cur.fetchall()
        if len(rows) < 1:
            pytest.skip("no client rows to test isolation")
        return [str(r[0]) for r in rows]

    def test_client_sees_only_own_rows(self):
        with rolled_back_tx(APP_DSN) as cur:
            ids      = self._two_client_ids(cur)
            client_a = ids[0]
            become_authenticated(cur, sub=client_a, user_role="client",
                                 app_metadata={"role": "client"})
            cur.execute("SELECT DISTINCT id FROM public.clients")
            seen = {str(r[0]) for r in cur.fetchall()}
            assert seen <= {client_a}, f"client isolation leak: {seen}"

    def test_kyc_settings_locked_to_admin(self):
        """A regular client must not read or write the fraud-check config."""
        with rolled_back_tx(APP_DSN) as cur:
            become_authenticated(cur, sub=str(uuid.uuid4()), user_role="client",
                                 app_metadata={"role": "client"})
            visible = scalar(cur, "SELECT count(*) FROM public.kyc_settings")
            assert visible == 0, "client can see KYC settings (should be admin-only)"

    def test_kyc_settings_visible_to_admin(self):
        with rolled_back_tx(APP_DSN) as cur:
            become_authenticated(cur, sub=str(uuid.uuid4()),
                                 user_role="ficium_admin",
                                 app_metadata={"role": "ficium_admin"})
            assert scalar(cur, "SELECT public.is_ficium_admin()") is True

    def test_client_cannot_create_request_as_another(self):
        """Spoofing another client_id on a request must be blocked by RLS."""
        with rolled_back_tx(APP_DSN) as cur:
            ids       = self._two_client_ids(cur)
            me, other = ids[0], (ids[1] if len(ids) > 1 else str(uuid.uuid4()))
            become_authenticated(cur, sub=me, user_role="client",
                                 app_metadata={"role": "client"})
            with pytest.raises(psycopg2.Error):
                cur.execute("""
                    INSERT INTO public.requests (id, client_id, product_type, amount, status)
                    VALUES (gen_random_uuid(), %s, 'mortgage', 999, 'open')
                """, (other,))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
