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

# Test tenant, SEEDED INSIDE EVERY rolled-back transaction (see rolled_back_tx).
# Until 2026-10-21 these pointed at live rows that no longer existed, so every
# test that needed a member failed the first time the suite actually ran.
# The suite must never depend on live data; nothing here persists.
MCB_INSTITUTION_ID  = "31b3ee32-2864-4875-8920-cc5f27240971"
MCB_ADMIN_AUTH_UID  = "4224540d-d59c-4584-8271-cb6ef24c472d"
MCB_ADMIN_MEMBER_ID = "ecf86654-4ef7-4fb5-aa8b-5545a4ff1d10"

requires_portal = pytest.mark.skipif(not PORTAL_DSN, reason="PORTAL_DB_DSN not set")
requires_app    = pytest.mark.skipif(not APP_DSN,    reason="APP_DB_DSN not set")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _seed_tenant(cur) -> None:
    """The test bank + its primary admin (institution_admin group), as the
    connection role, before any test switches to `authenticated`."""
    cur.execute("""
        INSERT INTO institution.institution
          (id, name, legal_name, institution_type, country, approved,
           onboarding_stage, primary_contact_email)
        VALUES (%s,'ZZ_SEED_BANK','ZZ Seed Bank','bank','MU',true,'approved','seed@zz.mu')
        ON CONFLICT (id) DO NOTHING
    """, (MCB_INSTITUTION_ID,))
    cur.execute("""
        INSERT INTO institution.member
          (id, institution_id, auth_user_id, email, full_name, role,
           is_primary_admin, active, group_id)
        VALUES (%s,%s,%s,'seed@zz.mu','Seed Admin','admin',true,true,
                (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin'))
        ON CONFLICT (id) DO NOTHING
    """, (MCB_ADMIN_MEMBER_ID, MCB_INSTITUTION_ID, MCB_ADMIN_AUTH_UID))
    make_compliant(cur, MCB_INSTITUTION_ID)


def make_compliant(cur, institution_id: str) -> None:
    """Give a test bank an approved, in-date document for every mandatory type,
    so the no-compliance-no-bids guard (db/050) lets it bid."""
    cur.execute("""
        INSERT INTO institution.doc
          (institution_id, doc_type_id, storage_path, file_name, mime_type, status, uploaded_at)
        SELECT %s, dt.id, 'zz/' || dt.code || '.pdf', dt.code || '.pdf', 'application/pdf',
               'approved', now()
          FROM institution.doc_type dt
         WHERE dt.is_mandatory
    """, (institution_id,))


def add_member(cur, *, admin: bool, modules_group: str | None = None) -> tuple[str, str]:
    """Add a member to the seed bank (as the connection role). admin=True puts
    them in institution_admin; otherwise a plain maker with no modules unless a
    portal_admin group slug is given. Returns (member_id, auth_uid)."""
    cur.execute("RESET ROLE")
    member_id, auth_uid = str(uuid.uuid4()), str(uuid.uuid4())
    slug = "institution_admin" if admin else modules_group
    cur.execute("""
        INSERT INTO institution.member
          (id, institution_id, auth_user_id, email, full_name, role,
           is_primary_admin, active, group_id)
        VALUES (%s,%s,%s,%s,'ZZ','member',false,true,
                (SELECT id FROM portal_admin.user_groups WHERE slug = %s))
    """, (member_id, MCB_INSTITUTION_ID, auth_uid, f"zz-{member_id[:8]}@zz.mu", slug))
    return member_id, auth_uid


@contextmanager
def rolled_back_tx(dsn: str):
    """Yield a cursor in a transaction that is always rolled back. On the
    Portal DB the test tenant is seeded first."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        if dsn == PORTAL_DSN:
            _seed_tenant(cur)
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

    def test_bank_user_cannot_insert_a_bid_directly(self):
        """Bids are platform-locked to four-eyes (db/040): a bank user may not
        write marketplace.bid at all, only submit bid.submit for approval."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            req, _ = _seed_catalog_and_request(cur)
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("""
                    INSERT INTO marketplace.bid
                      (request_id, institution_id, rate, rate_type, amount_offered,
                       term_months, submitted_via, idempotency_key)
                    VALUES (%s,%s,7.5,'fixed',500000,36,'portal',%s)
                """, (req, MCB_INSTITUTION_ID, "zzbid-" + req))

    def test_bid_submission_queues_for_a_second_person(self):
        with rolled_back_tx(PORTAL_DSN) as cur:
            req, _ = _seed_catalog_and_request(cur)
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval('bid.submit', 'request', %s, %s::jsonb)
            """, (req, json.dumps({"request_id": req, "rate": 7.5, "rate_type": "fixed",
                                   "amount_offered": 500000, "term_months": 36})))
            assert scalar(cur, "SELECT status FROM governance.action WHERE id = %s",
                          (action_id,)) == "pending"
            assert scalar(cur, "SELECT count(*) FROM marketplace.bid WHERE request_id = %s",
                          (req,)) == 0

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
            # A competing live bid from a SECOND bank (one bid per bank per
            # request), placed while the request is still open.
            bank_b = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.institution
                  (id, name, legal_name, institution_type, country, approved,
                   onboarding_stage, primary_contact_email)
                VALUES (%s,'ZZ_BANK_B','ZZ Bank B','bank','MU',true,'approved','b@zz.mu')
            """, (bank_b,))
            make_compliant(cur, bank_b)
            cur.execute("""
                INSERT INTO marketplace.bid
                  (request_id, institution_id, rate, rate_type, amount_offered,
                   term_months, submitted_via, idempotency_key)
                VALUES (%s,%s,6.5,'fixed',500000,36,'portal',%s) RETURNING id
            """, (req, bank_b, "zzdup2-" + req))
            other_bid = str(cur.fetchone()[0])

            cur.execute("SELECT marketplace.accept_bid(%s,%s,%s,%s::jsonb)",
                        (req, bid_id, consumer_id, phase2))
            # Same bid again: a retried contract call, harmless (db/046).
            again = scalar(cur, "SELECT marketplace.accept_bid(%s,%s,%s,%s::jsonb)",
                           (req, bid_id, consumer_id, phase2))
            assert again["repeat"] is True
            # A DIFFERENT bid after acceptance must be refused: before 046 it
            # flipped the winner while the borrower's PII stayed with bank A.
            expect_refused(cur, "SELECT marketplace.accept_bid(%s,%s,%s,%s::jsonb)",
                           (req, other_bid, consumer_id, phase2), "already accepted")
            assert str(scalar(cur, "SELECT winning_bid_id FROM marketplace.request WHERE id = %s",
                              (req,))) == bid_id


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
            # Force expires_at into the past. As the SETUP role: bank users may
            # not edit approval records at all (db/040), which is the point.
            cur.execute("RESET ROLE")
            cur.execute(
                "UPDATE governance.action SET expires_at = now() - interval '1 hour' WHERE id = %s",
                (action_id,),
            )
            # Approve as a different admin, who would otherwise be allowed
            _, checker_uid = add_member(cur, admin=True)
            become_authenticated(cur, sub=checker_uid,
                                 institution_id=MCB_INSTITUTION_ID)
            expect_refused(cur, "SELECT institution.approve_action(%s, 'late')",
                           (action_id,), "expired")


# ===========================================================================
# PORTAL DB — deal pipeline
# ===========================================================================
# TestPipelineFlow was removed on 2026-10-21. Its two tests ran plain UPDATEs
# as the superuser and asserted the UPDATE happened: they never called the
# pipeline code, so they would pass even if deal-stage four-eyes were broken.
# Real gap (not hidden by a fake test): deal-stage maker-checker is enforced
# only in app/api/pipeline.py, not in the database, and has no live test. It
# moves under governance.submit / Setup > Controls with the remaining modules.


# ===========================================================================
# PORTAL DB — benefits module
# ===========================================================================
@requires_portal
class TestBenefitsModule:

    def test_benefit_crud_scoped_to_institution(self):
        """Benefits are always institution-scoped; another institution cannot see them."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            # An existing benefit category (institution.benefit_cat)
            cat_id = scalar(cur, "SELECT id FROM institution.benefit_cat WHERE code = 'reward'")
            assert cat_id is not None, "benefit_cat 'reward' missing"

            # Seed a catalog product for FK
            fam_id  = str(uuid.uuid4())
            prod_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO catalog.product_family (id, code, label, sort_order)
                VALUES (%s, 'zz_bf', 'ZZ Benefit Fam', 998)
            """, (fam_id,))
            cur.execute("""
                INSERT INTO catalog.product
                    (id, family_id, code, label, currency, active, sort_order)
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

            # As different institution — must NOT see it (RLS). The other bank
            # is created by the SETUP role: bank users cannot create banks (038).
            other_uid = str(uuid.uuid4())
            other_iid = str(uuid.uuid4())
            cur.execute("RESET ROLE")
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


# ===========================================================================
# PORTAL DB — four-eyes switch (db/040-044), incl. the non-admin paths
# ===========================================================================
def expect_refused(cur, sql: str, params: tuple, contains: str) -> None:
    """Run a statement that MUST fail, inside a savepoint so the test's
    transaction survives, and check the refusal reason."""
    cur.execute("SAVEPOINT expect_refused")
    try:
        cur.execute(sql, params)
    except psycopg2.Error as e:
        cur.execute("ROLLBACK TO SAVEPOINT expect_refused")
        assert contains.lower() in str(e).lower(), f"refused for the wrong reason: {e}"
        return
    cur.execute("ROLLBACK TO SAVEPOINT expect_refused")
    raise AssertionError(f"expected refusal containing {contains!r}, but it succeeded")


def _switch_off(cur, category: str, checker_uid: str) -> None:
    """Seed admin requests switch-off; a second admin approves."""
    become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID, institution_id=MCB_INSTITUTION_ID)
    action_id = scalar(cur, """
        SELECT institution.submit_for_approval('four_eyes.disable', 'four_eyes_policy',
                                               NULL, %s::jsonb)
    """, (json.dumps({"category": category}),))
    become_authenticated(cur, sub=checker_uid, institution_id=MCB_INSTITUTION_ID)
    cur.execute("SELECT institution.approve_action(%s, 'ok')", (action_id,))


@requires_portal
class TestFourEyesControls:

    def test_locked_control_cannot_be_switched_off(self):
        with rolled_back_tx(PORTAL_DSN) as cur:
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID, institution_id=MCB_INSTITUTION_ID)
            expect_refused(cur, """
                SELECT institution.submit_for_approval('four_eyes.disable', 'four_eyes_policy',
                                                       NULL, '{"category":"user.create"}'::jsonb)
            """, (), "locked")

    def test_non_admin_cannot_request_a_switch_off(self):
        with rolled_back_tx(PORTAL_DSN) as cur:
            _, maker_uid = add_member(cur, admin=False)
            become_authenticated(cur, sub=maker_uid, institution_id=MCB_INSTITUTION_ID)
            expect_refused(cur, """
                SELECT institution.submit_for_approval('four_eyes.disable', 'four_eyes_policy',
                                                       NULL, '{"category":"benefit.create"}'::jsonb)
            """, (), "only institution admins")

    def test_non_admin_cannot_approve_a_switch_off(self):
        """db/044: weakening a control needs an ADMIN checker, not any member."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            _, maker_uid = add_member(cur, admin=False)
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID, institution_id=MCB_INSTITUTION_ID)
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval('four_eyes.disable', 'four_eyes_policy',
                                                       NULL, '{"category":"benefit.create"}'::jsonb)
            """)
            become_authenticated(cur, sub=maker_uid, institution_id=MCB_INSTITUTION_ID)
            expect_refused(cur, "SELECT institution.approve_action(%s, 'ok')",
                           (action_id,), "only an institution admin")

    def test_switched_off_action_runs_once_and_is_recorded_single(self):
        with rolled_back_tx(PORTAL_DSN) as cur:
            _, checker_uid = add_member(cur, admin=True)
            _switch_off(cur, "benefit.create", checker_uid)
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID, institution_id=MCB_INSTITUTION_ID)
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval('benefit.create', 'benefit', NULL,
                  jsonb_build_object('cat_id', (SELECT id FROM institution.benefit_cat
                                                 WHERE code = 'reward' LIMIT 1),
                                     'title', 'ZZ single', 'value_amount', 1, 'value_unit', 'mur'))
            """)
            cur.execute("SELECT status, control FROM governance.action WHERE id = %s", (action_id,))
            assert cur.fetchone() == ("approved", "single")

    def test_single_control_still_requires_the_screens_module(self):
        """db/040: with four-eyes off, a maker WITHOUT the module is refused."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            _, checker_uid = add_member(cur, admin=True)
            _, maker_uid = add_member(cur, admin=False)
            _switch_off(cur, "benefit.create", checker_uid)
            become_authenticated(cur, sub=maker_uid, institution_id=MCB_INSTITUTION_ID)
            expect_refused(cur, """
                SELECT institution.submit_for_approval('benefit.create', 'benefit', NULL,
                  jsonb_build_object('cat_id', (SELECT id FROM institution.benefit_cat
                                                 WHERE code = 'reward' LIMIT 1),
                                     'title', 'ZZ refused', 'value_amount', 1, 'value_unit', 'mur'))
            """, (), "you do not have access")

    def test_bank_user_cannot_forge_an_approval(self):
        with rolled_back_tx(PORTAL_DSN) as cur:
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID, institution_id=MCB_INSTITUTION_ID)
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval('benefit.delete', 'benefit', NULL,
                                                       '{"benefit_id":"00000000-0000-0000-0000-000000000000"}'::jsonb)
            """)
            expect_refused(cur, "UPDATE governance.action SET status = 'approved' WHERE id = %s",
                           (action_id,), "permission denied")



# ===========================================================================
# APP DB — Offers feed. Design as of 2026-10-25 (admin_legacy_retire_v1):
# the admin DB (Neon) is the master for offers; its outbox worker calls ONE
# function here, admin.sync_apply, as role ficium_admin_sync. Borrowers read
# public.offer_listing only, live rows only. The old App DB write path
# (admin.offer_*, role ficium_admin_app) is gone and must stay gone.
# ===========================================================================
_SYNC_ROLE = "ficium_admin_sync"
_SYNC_APPLY = "admin.sync_apply(uuid,text,text,text,bigint,jsonb)"
_SYNC_HELPERS = (
    "admin._sync_offer_upsert(jsonb)",
    "admin._sync_kyc(uuid,jsonb)",
    "admin._sync_config(jsonb)",
)
_RETIRED_FUNCS = (
    "admin.offer_save(uuid,jsonb,text,integer)",
    "admin.offer_approve(uuid,text,text)",
    "admin.offer_publish(uuid,text)",
    "admin.offer_withdraw(uuid,text,text)",
    "admin.offer_reverify(uuid,text,timestamptz,text)",
)
_CLIENT = {"role": "client"}


def _insert_listing(cur, *, live_until_sql: str) -> str:
    """Insert a listing as the connection role (owner), bypassing the sync path."""
    oid = str(uuid.uuid4())
    cur.execute(f"""
        INSERT INTO public.offer_listing
          (id, institution_name, category, product_type, caption, headline, terms,
           is_launch, published_at, verified_at, live_until)
        VALUES (%s, 'ZZ Test Bank', 'save', 'savings_plan', 'zz integration caption',
                'h', 't', false, now() - interval '5 days', now() - interval '1 day',
                {live_until_sql})
    """, (oid,))
    return oid


@requires_app
class TestAppOffers:

    def test_borrower_sees_live_offers_only(self):
        with rolled_back_tx(APP_DSN) as cur:
            live = _insert_listing(cur, live_until_sql="now() + interval '10 days'")
            stale = _insert_listing(cur, live_until_sql="now() - interval '10 days'")
            become_authenticated(cur, sub=str(uuid.uuid4()), user_role="client",
                                 app_metadata=_CLIENT)
            cur.execute("SELECT id::text FROM public.offer_listing WHERE id IN (%s, %s)",
                        (live, stale))
            seen = {r[0] for r in cur.fetchall()}
            assert seen == {live}, "expired offer visible to borrower"

    def test_borrower_cannot_write_or_reach_admin(self):
        with rolled_back_tx(APP_DSN) as cur:
            oid = _insert_listing(cur, live_until_sql="now() + interval '10 days'")
            become_authenticated(cur, sub=str(uuid.uuid4()), user_role="client",
                                 app_metadata=_CLIENT)
            denied = "permission denied"
            expect_refused(cur, "UPDATE public.offer_listing SET headline = 'zz' WHERE id = %s",
                           (oid,), denied)
            expect_refused(cur, "DELETE FROM public.offer_listing WHERE id = %s", (oid,), denied)
            expect_refused(cur, "SELECT count(*) FROM admin.sync_log", (), denied)
            expect_refused(cur, "SELECT count(*) FROM admin.sync_state", (), denied)
            expect_refused(
                cur, "SELECT admin.sync_apply(%s, 'x', 'x', 'x', 1, '{}'::jsonb)",
                (str(uuid.uuid4()),), denied)

    def test_anon_reads_nothing(self):
        with rolled_back_tx(APP_DSN) as cur:
            _insert_listing(cur, live_until_sql="now() + interval '10 days'")
            cur.execute("SET LOCAL ROLE anon")
            expect_refused(cur, "SELECT count(*) FROM public.offer_listing", (),
                           "permission denied")

    def test_old_app_db_write_path_is_retired(self):
        """admin_legacy_retire_v1: nothing on the App DB may edit offers except
        admin.sync_apply. If any of this reappears, the single audited path is bypassed."""
        with rolled_back_tx(APP_DSN) as cur:
            for f in _RETIRED_FUNCS:
                assert scalar(cur, "SELECT to_regprocedure(%s)", (f,)) is None, f
            for t in ("admin.offer", "admin.offer_change"):
                assert scalar(cur, "SELECT to_regclass(%s)", (t,)) is None, t
            assert not scalar(cur, "SELECT EXISTS (SELECT 1 FROM pg_roles "
                                   "WHERE rolname = 'ficium_admin_app')")

    def test_sync_role_can_only_apply(self):
        with rolled_back_tx(APP_DSN) as cur:
            fn_priv = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
            assert scalar(cur, fn_priv, (_SYNC_ROLE, _SYNC_APPLY)), "sync role cannot apply"
            for r in ("authenticated", "anon"):
                assert not scalar(cur, fn_priv, (r, _SYNC_APPLY)), r
            for h in _SYNC_HELPERS:  # internals: not even the sync role calls them directly
                for r in (_SYNC_ROLE, "authenticated", "anon"):
                    assert not scalar(cur, fn_priv, (r, h)), (r, h)
            tbl_priv = f"SELECT has_table_privilege('{_SYNC_ROLE}', %s, %s)"
            for t in ("public.offer_listing", "admin.sync_log", "admin.sync_state"):
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not scalar(cur, tbl_priv, (t, priv)), (t, priv)

    def test_sync_log_is_append_only(self):
        with rolled_back_tx(APP_DSN) as cur:
            cur.execute("""
                INSERT INTO admin.sync_log
                  (event_id, topic, aggregate_type, aggregate_id, aggregate_version, outcome)
                VALUES (%s, 'zz.test', 'zz', 'zz', 1, 'applied')
            """, (str(uuid.uuid4()),))
            expect_refused(cur, "DELETE FROM admin.sync_log", (), "append-only")
            expect_refused(cur, "UPDATE admin.sync_log SET topic = 'zz'", (), "append-only")


@requires_portal
class TestComplianceGate:

    def test_bank_without_approved_documents_cannot_bid(self):
        """db/050: no approved, in-date required documents, no bids, on every path."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            req, _ = _seed_catalog_and_request(cur)
            bare = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.institution
                  (id, name, legal_name, institution_type, country, approved,
                   onboarding_stage, primary_contact_email)
                VALUES (%s,'ZZ_BARE','ZZ Bare','commercial_bank','MU',true,'approved','x@zz.mu')
            """, (bare,))
            expect_refused(cur, """
                INSERT INTO marketplace.bid
                  (request_id, institution_id, rate, rate_type, amount_offered,
                   term_months, submitted_via, idempotency_key)
                VALUES (%s,%s,7.0,'fixed',500000,36,'portal',%s)
            """, (req, bare, "zzbare-" + req), "cannot bid yet")


@requires_portal
def test_no_definer_function_executable_by_anon() -> None:
    """Security audit 2026-10-24 / db/053: Postgres grants EXECUTE to PUBLIC on every
    new function, so a new SECURITY DEFINER function is callable with the public anon
    key unless its migration revokes it. Fails until it does."""
    with rolled_back_tx(PORTAL_DSN or "") as cur:
        cur.execute("""
        SELECT n.nspname || '.' || p.proname
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE p.prosecdef AND has_function_privilege('anon', p.oid, 'EXECUTE')
           AND n.nspname NOT IN ('pg_catalog','information_schema','auth','storage','realtime',
                                 'extensions','graphql','graphql_public','pgsodium','vault','net',
                                 'cron','supabase_functions','pgbouncer')""")
        leaked = [r[0] for r in cur.fetchall()]
    assert not leaked, f"REVOKE EXECUTE ... FROM public, anon on: {leaked}"
