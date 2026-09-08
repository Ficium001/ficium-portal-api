# =============================================================================
# ficium-portal-api — Borrower portfolio module
# Prefix: /marketplace/borrower  (Ficium App-authenticated, not ficium-auth)
#
# Endpoints:
#   GET /marketplace/borrower/portfolio — the calling borrower's accepted
#     facilities (marketplace.loan_pipeline) with borrower-visible stage
#     progress. Read-only.
#
# Architecture notes:
#   - Authenticated via current_app_user (Ficium App Supabase token —
#     see core/app_auth.py), NOT current_claims (ficium-auth). Institution
#     DB has no RLS concept of a borrower, so this uses service_session
#     with an explicit consumer_id guard on every query — same pattern
#     request_chat.py uses for institution_id scoping.
#   - Institution identity (name) IS revealed here: bid_acceptance already
#     reveals the borrower's identity to the institution post-acceptance
#     (see pipeline.py's Phase 2 reveal), so the reverse reveal to the
#     borrower is symmetric, not a new disclosure.
#   - Stage instances filtered to borrower_visible=true stage defs only —
#     internal-only stages (e.g. board approval mechanics) stay hidden.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ..core.db import service_session
from ..deps import current_app_user

router = APIRouter(prefix="/marketplace/borrower", tags=["borrower-portfolio"])


def _rows(result) -> list[dict]: return [dict(r._mapping) for r in result.fetchall()]


@router.get("/portfolio")
async def get_borrower_portfolio(
    user: dict = Depends(current_app_user),
) -> dict:
    """
    All accepted facilities for the calling borrower, each with its
    borrower-visible stage progress. Ordered most-recently-started first.
    """
    consumer_id = user["id"]

    with service_session() as conn:
        facilities = _rows(conn.execute(text("""
            SELECT
                lp.id,
                lp.request_id,
                lp.bid_id,
                lp.status,
                lp.deal_amount,
                lp.deal_rate,
                lp.deal_term_months,
                lp.started_at,
                lp.completed_at,
                cp.label        AS product_label,
                r.currency,
                inst.name       AS institution_name,
                inst.logo_url   AS institution_logo_url
            FROM marketplace.loan_pipeline lp
            JOIN marketplace.request r        ON r.id = lp.request_id
            LEFT JOIN catalog.product cp       ON cp.id = r.product_id
            LEFT JOIN institution.institution inst ON inst.id = lp.institution_id
            WHERE lp.consumer_id = :cid
            ORDER BY lp.started_at DESC
        """), {"cid": consumer_id}))

        if not facilities:
            return {"facilities": []}

        pipeline_ids = [f["id"] for f in facilities]
        stages = _rows(conn.execute(text("""
            SELECT
                psi.pipeline_id,
                psi.position,
                psi.status,
                psi.started_at,
                psi.completed_at,
                psi.sla_due_at,
                psd.label,
                psd.borrower_label,
                (psi.sla_due_at IS NOT NULL
                 AND psi.sla_due_at < now()
                 AND psi.status NOT IN ('completed', 'skipped')) AS sla_breached
            FROM marketplace.pipeline_stage_instance psi
            JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
            WHERE psi.pipeline_id = ANY(:pids) AND psd.borrower_visible = true
            ORDER BY psi.pipeline_id, psi.position
        """), {"pids": pipeline_ids}))

    stages_by_pipeline: dict[str, list[dict]] = {}
    for s in stages:
        stages_by_pipeline.setdefault(s["pipeline_id"], []).append(s)

    for f in facilities:
        f["stages"] = stages_by_pipeline.get(f["id"], [])

    return {"facilities": facilities}
