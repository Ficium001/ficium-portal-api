"""
Go-live readiness for the pilot bank. Separate from the regression suite on
purpose: these describe the state of live configuration, not code, and stay red
until someone fixes the configuration. They run in their own CI job so they are
visible every day without blocking unrelated pull requests.
"""
from __future__ import annotations

import psycopg2

from tests.test_integration import PORTAL_DSN, requires_portal

MCB = "f192050a-dfdd-4da7-874e-c3db88f11e41"
pytestmark = requires_portal


def test_mcb_can_bid_today() -> None:
    """Go-live check: the pilot bank passes the no-documents-no-bids gate (db/050)."""
    conn = psycopg2.connect(PORTAL_DSN or "")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT can_bid, missing_docs, waived_until FROM institution.compliance "
            "WHERE institution_id = %s",
            (MCB,),
        )
        can_bid, missing, waived = cur.fetchone()
        assert can_bid, (
            f"MCB cannot bid: missing approved documents {missing}, "
            f"no waiver ({waived}). Upload them or grant a time-limited waiver."
        )
    finally:
        conn.close()
