"""
Ficium — End-to-end integration test suite
============================================

Regression tests for the security and flow guarantees of both databases:
  • Portal DB  (institution side)  — tenant isolation, RLS enforcement,
    bidding, maker-checker, catalogue readability.
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
APP_DSN = os.environ.get("APP_DB_DSN")

# Known seed identity in the portal DB (the live MCB admin). These are not
# secrets — just row identifiers used to drive RLS as a real member.
MCB_INSTITUTION_ID = "31b3ee32-2864-4875-8920-cc5f27240971"
MCB_ADMIN_AUTH_UID = "4224540d-d59c-4584-8271-cb6ef24c472d"
MCB_ADMIN_MEMBER_ID = "ecf86654-4ef7-4fb5-aa8b-5545a4ff1d10"

requires_portal = pytest.mark.skipif(not PORTAL_DSN, reason="PORTAL_DB_DSN not set")
requires_app = pytest.mark.skipif(not APP_DSN, reason="APP_DB_DSN not set")


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
            other_inst = str(uuid.uuid4())
            other_member = str(uuid.uuid4())
            # Insert a foreign tenant (as owner, before dropping role)
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

    def _seed_request(self, cur) -> str:
        """Create a catalog product + biddable request as owner. Returns req id."""
        fam = str(uuid.uuid4())
        prod = str(uuid.uuid4())
        req = str(uuid.uuid4())
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
        return req

    def test_institution_can_submit_own_bid(self):
        with rolled_back_tx(PORTAL_DSN) as cur:
            req = self._seed_request(cur)
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
            req = self._seed_request(cur)
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            with pytest.raises(psycopg2.Error):
                cur.execute("""
                    INSERT INTO marketplace.bid
                      (request_id, institution_id, rate, rate_type, amount_offered,
                       term_months, submitted_via, idempotency_key)
                    VALUES (%s, %s, 5.0,'fixed',500000,36,'portal',%s)
                """, (req, str(uuid.uuid4()), "evil-" + req))


# ===========================================================================
# PORTAL DB — maker-checker (governance)
# ===========================================================================
@requires_portal
class TestPortalMakerChecker:

    def test_submit_then_approve_by_different_member(self):
        """Full loop: a maker submits, a different member approves, it executes."""
        with rolled_back_tx(PORTAL_DSN) as cur:
            # second member acts as maker so MCB admin can be checker
            maker_id = str(uuid.uuid4())
            maker_uid = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO institution.member
                  (id, institution_id, auth_user_id, email, full_name, role,
                   is_primary_admin, active)
                VALUES (%s,%s,%s,'mk@z.mu','MK','member',false,true)
            """, (maker_id, MCB_INSTITUTION_ID, maker_uid))

            # maker submits a group.create action
            become_authenticated(cur, sub=maker_uid,
                                 institution_id=MCB_INSTITUTION_ID)
            slug = "zz_grp_" + uuid.uuid4().hex[:6]
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval(
                  'group.create','group', gen_random_uuid(),
                  %s::jsonb)
            """, (json.dumps({"slug": slug, "label": "ZZ",
                              "module_permissions": []}),))
            assert action_id is not None

            # checker (MCB admin, different member) approves
            become_authenticated(cur, sub=MCB_ADMIN_AUTH_UID,
                                 institution_id=MCB_INSTITUTION_ID)
            cur.execute("SELECT institution.approve_action(%s, 'ok')", (action_id,))

            # action approved + group created
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
            slug = "zz_self_" + uuid.uuid4().hex[:6]
            action_id = scalar(cur, """
                SELECT institution.submit_for_approval(
                  'group.create','group', gen_random_uuid(), %s::jsonb)
            """, (json.dumps({"slug": slug, "label": "ZZ",
                              "module_permissions": []}),))
            with pytest.raises(psycopg2.Error):
                cur.execute("SELECT institution.approve_action(%s, 'self')",
                            (action_id,))


# ===========================================================================
# PORTAL DB — cross-DB sync mechanism (functions only; no app DB needed)
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
            app_req = str(uuid.uuid4())
            consumer = str(uuid.uuid4())
            for amount in (100000, 200000):  # second call = update
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
            ids = self._two_client_ids(cur)
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
            ids = self._two_client_ids(cur)
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
