from __future__ import annotations

import asyncio
import hmac
import json
from datetime import UTC

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.db import AppDatabaseUnavailable, app_service_session, service_session
from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..core.webhooks import dispatch_event
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

def _row(r) -> dict: return dict(r._mapping)

# ── GET /marketplace/requests ─────────────────────────────────────────────────
@router.get("/requests")
async def list_requests(
    product_type: str | None = Query(default=None),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    sql = """
        SELECT r.id, r.consumer_ref, r.product_id,
               p.label   AS product_label,
               pf.label  AS product_family_label,
               r.country, r.currency, r.amount, r.term_months,
               r.params, r.metadata, r.status,
               r.bid_window_opens_at, r.bid_window_closes_at,
               r.source, r.created_at,
               r.ficium_risk_tier, r.ficium_score,
               (SELECT COUNT(*) FROM marketplace.bid b
                WHERE b.request_id = r.id
                  AND b.status NOT IN ('withdrawn','rejected')) AS bid_count
        FROM marketplace.request r
        JOIN catalog.product        p  ON p.id  = r.product_id
        JOIN catalog.product_family pf ON pf.id = p.family_id
        WHERE r.status IN ('open', 'bidding')
          AND r.bid_window_closes_at > now()
          AND r.bid_window_opens_at  <= now()
    """
    params: dict = {}
    if product_type:
        sql += " AND p.code = :pt"
        params["pt"] = product_type
    sql += " ORDER BY r.bid_window_closes_at ASC"
    rows = conn.execute(text(sql), params).fetchall()
    # No fallback to the App DB here: marketplace.request (Portal DB) is the
    # single source of truth post Phase-1-sync, and is RLS-scoped by
    # institution + product_scope. A raw App DB fallback previously bypassed
    # both — every open request, every institution, every product, with no
    # filtering at all — and triggered any time a member's scoped query
    # legitimately returned zero rows (e.g. a product-restricted group with
    # no current matches). Removed rather than "fixed to be scoped" so there
    # is exactly one authorization path to maintain, not two that can drift.
    return [_row(r) for r in rows]

# ── GET /marketplace/my-bids ──────────────────────────────────────────────────
@router.get("/my-bids")
async def list_my_bids(
    status: str | None = Query(default=None),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    if claims.get("user_role") in INSTITUTION_ADMIN_ROLES:
        return []
    sql = "SELECT * FROM marketplace.my_bids"
    params: dict = {}
    if status:
        sql += " WHERE status = :st"
        params["st"] = status
    sql += " ORDER BY submitted_at DESC"
    return [_row(r) for r in conn.execute(text(sql), params).fetchall()]

# ── POST /marketplace/bids ────────────────────────────────────────────────────
@router.post("/bids")
async def submit_bid(
    body: dict = Body(...),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    required = {"request_id", "rate", "rate_type", "amount_offered", "term_months"}
    missing = required - set(body.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    # Window guard — check before hitting the DB trigger
    req_row = conn.execute(
        text("SELECT status, bid_window_closes_at FROM marketplace.request WHERE id = :rid"),
        {"rid": body["request_id"]},
    ).fetchone()
    if req_row is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if req_row.status != "bidding":
        raise HTTPException(status_code=409, detail=f"Request is not open for bidding (status: {req_row.status}).")
    from datetime import datetime
    if req_row.bid_window_closes_at and req_row.bid_window_closes_at < datetime.now(UTC):
        raise HTTPException(status_code=409, detail="Bid window has closed for this request.")

    try:
        result = conn.execute(text("""
            INSERT INTO marketplace.bid
                (request_id, institution_id, rate, rate_type, rate_valid_days,
                 amount_offered, term_months, conditions, fee_structure,
                 submitted_via, idempotency_key)
            VALUES
                (:request_id, :institution_id, :rate, :rate_type, :rate_valid_days,
                 :amount_offered, :term_months, CAST(:conditions AS jsonb),
                 CAST(:fee_structure AS jsonb), 'portal', :idempotency_key)
            ON CONFLICT (institution_id, request_id, idempotency_key) DO NOTHING
            RETURNING id, status, submitted_at
        """), {
            "request_id":      body["request_id"],
            "institution_id":  inst_id,
            "rate":            body["rate"],
            "rate_type":       body.get("rate_type", "fixed"),
            "rate_valid_days": body.get("rate_valid_days"),
            "amount_offered":  body["amount_offered"],
            "term_months":     body["term_months"],
            "conditions":      json.dumps(body.get("conditions", {})),
            "fee_structure":   json.dumps(body.get("fee_structure", {})),
            "idempotency_key": body.get("idempotency_key"),
        }).fetchone()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if result is None:
        return {"status": "duplicate", "message": "Bid already submitted."}
    return {"id": str(result.id), "status": result.status, "submitted_at": result.submitted_at.isoformat()}

# ── GET /marketplace/bids/{bid_id} ───────────────────────────────────────────
@router.get("/bids/{bid_id}")
async def get_bid(bid_id: str, conn: Session = Depends(tenant_conn)) -> dict:
    row = conn.execute(text("SELECT * FROM marketplace.my_bids WHERE id = :id"), {"id": bid_id}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Bid not found.")
    return _row(row)

# ── POST /marketplace/requests/{request_id}/reject ──────────────────────────
@router.post("/requests/{request_id}/reject")
async def reject_request(
    request_id: str,
    body: dict = Body(default={}),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict:
    """
    Institution explicitly declines a marketplace request (distinct from
    simply not bidding on it). The App DB's reject_request() SECURITY
    DEFINER function is the single authority for the status transition,
    the borrower notification, and the audit trail (request_decisions) —
    this endpoint only validates institution visibility/eligibility first,
    then mirrors the decline onto the Portal DB copy so it drops out of
    every institution's open queue immediately.
    """
    inst_id = claims.get("institution_id")
    if not inst_id:
        raise HTTPException(status_code=403, detail="No institution context.")

    reason = (body.get("reason") or "").strip() or None
    if reason and len(reason) > 500:
        raise HTTPException(status_code=422, detail="Reason too long (max 500 chars).")

    # RLS-scoped visibility/state pre-check before the App DB round-trip.
    req_row = conn.execute(
        text("SELECT id, status FROM marketplace.request WHERE id = :rid"),
        {"rid": request_id},
    ).fetchone()
    if req_row is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if req_row.status != "bidding":
        raise HTTPException(status_code=409, detail=f"Request is not open (status: {req_row.status}).")

    try:
        with app_service_session() as app_conn:
            result = app_conn.execute(
                text("SELECT public.reject_request(CAST(:rid AS uuid), CAST(:iid AS uuid), :reason) AS result"),
                {"rid": request_id, "iid": inst_id, "reason": reason},
            ).fetchone()
    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    payload = result.result if result else {"ok": False, "error": "no_response"}
    if not payload.get("ok"):
        err = payload.get("error", "reject_failed")
        status_code = 409 if err in ("request_not_open", "request_not_found") else 400
        raise HTTPException(status_code=status_code, detail=err)

    # Mirror onto the Portal DB copy — don't wait on the next pull-sync,
    # which only ever ingests App DB requests that are still 'open'.
    conn.execute(
        text("""
            UPDATE marketplace.request
            SET status = 'cancelled', cancelled_at = now(), cancellation_reason = :reason
            WHERE id = :rid
        """),
        {"rid": request_id, "reason": reason or "Declined by institution"},
    )

    asyncio.create_task(dispatch_event(str(inst_id), "request.rejected", {
        "request_id": request_id,
        "reason": reason,
    }))

    # Best-effort audit trail entry — never blocks or fails the reject itself.
    try:
        with service_session() as audit_conn:
            audit_conn.execute(
                text("""
                    INSERT INTO audit.event
                        (id, occurred_at, actor_id, actor_type, actor_email, actor_role,
                         institution_id, action, resource_type, resource_id, outcome, metadata)
                    VALUES
                        (gen_random_uuid(), now(), CAST(:actor_id AS uuid), 'member', :actor_email, :actor_role,
                         CAST(:inst_id AS uuid), 'request.reject', 'marketplace_request', CAST(:rid AS uuid),
                         'success', jsonb_build_object('reason', :reason))
                """),
                {
                    "actor_id": claims.get("sub"),
                    "actor_email": claims.get("email"),
                    "actor_role": claims.get("user_role"),
                    "inst_id": inst_id,
                    "rid": request_id,
                    "reason": reason,
                },
            )
    except Exception:
        pass  # audit is best-effort; core action already committed

    return {"ok": True, "request_id": request_id, "status": "rejected"}

# ── POST /marketplace/close-expired ──────────────────────────────────────────
def _verify_service_secret(received: str) -> None:
    expected = settings.app_service_secret
    if not expected:
        raise HTTPException(status_code=503, detail="Service secret not configured.")
    if not hmac.compare_digest(received.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid service secret.")

@router.post("/close-expired")
async def close_expired(
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
) -> dict:
    _verify_service_secret(x_service_secret)

    # Collect bids on about-to-expire requests BEFORE closing them
    # so we can notify each institution whose bid was rejected
    with service_session() as conn:
        rejected_bids = conn.execute(text("""
            SELECT b.institution_id, b.id AS bid_id, b.request_id
            FROM marketplace.bid b
            JOIN marketplace.request r ON r.id = b.request_id
            WHERE r.status = 'open'
              AND r.bid_window_closes_at < now()
              AND b.status IN ('submitted', 'under_review')
        """)).fetchall()

        result = conn.execute(text("SELECT marketplace.close_expired_windows() AS closed")).fetchone()

    closed = result.closed if result else 0

    # Fire bid.rejected webhooks for each affected institution
    for bid in rejected_bids:
        asyncio.create_task(dispatch_event(
            str(bid.institution_id),
            "bid.rejected",
            {
                "bid_id":     str(bid.bid_id),
                "request_id": str(bid.request_id),
                "reason":     "request_expired",
            },
        ))

    return {"closed": closed, "message": f"Closed {closed} expired bid window(s).", "bids_rejected": len(rejected_bids)}

# ── POST /marketplace/sync-requests ──────────────────────────────────────────
# Phase 1 helpers ─────────────────────────────────────────────────────────────
def _parse_purpose(purpose: str | None) -> dict:
    if not purpose:
        return {}
    out: dict = {}
    for part in purpose.split("|"):
        part = part.strip()
        if ":" in part:
            key, _, val = part.partition(":")
            out[key.strip().lower().replace(" ", "_")] = val.strip()
    return out

def _net_worth_band(nw: float | None) -> str | None:
    if nw is None:
        return None
    if nw < 0:
        return "negative"
    if nw < 500_000:
        return "< 500k"
    if nw < 1_000_000:
        return "500k-1M"
    if nw < 5_000_000:
        return "1M-5M"
    return "> 5M"

def _risk_tier(risk_score: int | None) -> str | None:
    if risk_score is None:
        return None
    if risk_score < 20:
        return "A"
    if risk_score < 40:
        return "B"
    if risk_score < 60:
        return "C"
    return "D"

def _collateral(product_type: str, parsed: dict) -> tuple[str | None, str | None]:
    pt = (product_type or "").lower()
    if pt in ("mortgage", "home_loan"):
        prop = (parsed.get("property_type") or "").lower()
        sub  = parsed.get("property_type")
        return ("land", sub) if "land" in prop else ("residential_property", sub)
    if pt in ("auto", "vehicle", "car_loan"):
        return "vehicle", parsed.get("vehicle_make") or parsed.get("vehicle_type")
    if pt in ("business", "business_loan"):
        return "business_asset", None
    return "none", None

def _ltv(amount: float, parsed: dict) -> float | None:
    try:
        asset_val = float(parsed.get("property_value") or parsed.get("vehicle_value") or 0)
        if asset_val > 0:
            return round((amount / asset_val) * 100, 1)
    except (TypeError, ValueError):
        pass
    return None

def _dsr(monthly_income, monthly_loan_payments, amount, term_months, parsed) -> tuple[float | None, float | None]:
    if not monthly_income:
        return None, None
    existing = monthly_loan_payments
    if existing is None:
        try:
            existing = float(parsed.get("monthly_debt") or 0)
        except (TypeError, ValueError):
            existing = 0.0
    existing = existing or 0.0
    dsr_current = round((existing / monthly_income) * 100, 1)
    dsr_post = round(((existing + amount / term_months) / monthly_income) * 100, 1) if term_months else None
    return dsr_current, dsr_post

def _build_phase1(r, parsed: dict) -> dict:
    income        = float(r.monthly_income or r.snap_income or 0) or None
    net_worth     = float(r.snap_net_worth or r.total_net_worth or 0) or None
    monthly_px    = float(r.monthly_loan_payments or 0) or None
    col_type, col_sub = _collateral(r.product_type, parsed)
    dsr_cur, dsr_post = _dsr(income, monthly_px, float(r.amount), r.preferred_term_months, parsed)
    ltv = _ltv(float(r.amount), parsed) if col_type not in (None, "none") else None

    loan_balance = None
    try:
        loan_balance = float((r.mortgage_balance or 0) + (r.personal_loan_balance or 0) +
                             (r.credit_card_balance or 0) + (r.vehicle_loan_balance or 0)) or None
    except (TypeError, ValueError):
        pass

    employer = r.emp_employer_name or r.dossier_employer

    phase1 = {
        "loan_purpose":               parsed.get("purpose"),
        "collateral_type":            col_type,
        "collateral_sub":             col_sub,
        "ltv_pct":                    ltv,
        "kyc_verified":               r.kyc_status == "verified",
        "employment_status":          r.employment_status,
        "employment_type":            r.employment_type,
        "employer":                   employer,
        "years_employed":             float(r.years_of_employment) if r.years_of_employment else None,
        "gross_monthly_income":       income,
        "income_verified":            r.kyc_status == "verified",
        "dsr_current_pct":            dsr_cur,
        "dsr_post_pct":               dsr_post,
        "net_worth_band":             _net_worth_band(net_worth),
        "has_existing_loans":         r.has_existing_loans,
        "existing_monthly_repayment": monthly_px,
        "existing_loan_balance":      loan_balance,
        "loan_breakdown":             r.loan_breakdown,
        "health_score":               r.health_score,
        "risk_score":                 r.risk_score,
        "affordability_score":        r.affordability_score,
        "risk_tier":                  _risk_tier(r.risk_score),
        "age":                        int(r.client_age) if r.client_age else None,
    }
    return {k: v for k, v in phase1.items() if v is not None}

_ENRICH_SQL = """
    SELECT
        r.id,
        r.client_id,
        r.product_type::text        AS product_type,
        r.amount,
        r.preferred_term_months,
        r.purpose,
        r.max_rate,
        r.decision_deadline,
        r.status::text              AS status,
        r.created_at,
        c.kyc_status,
        EXTRACT(YEAR FROM AGE(c.date_of_birth))::int AS client_age,
        cd.employment_status,
        cd.employer_name            AS dossier_employer,
        cd.monthly_income,
        cd.total_net_worth,
        cd.has_existing_loans,
        cd.health_score,
        cd.risk_score,
        cd.affordability_score,
        ed.employer_name            AS emp_employer_name,
        ed.employment_type,
        ed.years_of_employment,
        s.monthly_loan_payments,
        s.monthly_income            AS snap_income,
        s.net_worth                 AS snap_net_worth,
        s.mortgage_balance,
        s.personal_loan_balance,
        s.credit_card_balance,
        s.vehicle_loan_balance,
        (
            SELECT json_agg(json_build_object(
                'type',        l.loan_type,
                'outstanding', l.outstanding_amount,
                'monthly',     l.monthly_repayment,
                'bank',        l.bank_name,
                'months_left', l.remaining_months
            ) ORDER BY l.outstanding_amount DESC)
            FROM public.client_loan_details l
            WHERE l.client_id = r.client_id
        ) AS loan_breakdown
    FROM  public.requests r
    LEFT JOIN public.clients                   c  ON c.id         = r.client_id
    LEFT JOIN public.client_dossier            cd ON cd.client_id = r.client_id
    LEFT JOIN public.employment_details        ed ON ed.user_id   = r.client_id
    LEFT JOIN public.client_financial_snapshot s  ON s.client_id  = r.client_id
    WHERE r.status = 'open'
    ORDER BY r.created_at DESC
    LIMIT :lim
"""

_INGEST_SQL = """
    SELECT marketplace.ingest_app_request(
        :id, :consumer_id, :product_type, :amount, :term,
        :max_rate, :deadline, :status, :created_at,
        CAST(:phase1 AS jsonb)
    )
"""

@router.post("/sync-requests")
async def sync_requests(
    x_service_secret: str = Header(default="", alias="X-Service-Secret"),
    limit: int = Query(default=200, le=1000),
) -> dict:
    _verify_service_secret(x_service_secret)
    try:
        with app_service_session() as app_conn:
            app_rows = app_conn.execute(text(_ENRICH_SQL), {"lim": limit}).fetchall()
    except AppDatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    synced, failed = 0, 0
    errors: list[str] = []
    new_requests: list[dict] = []  # collect for webhook dispatch
    with service_session() as conn:
        for r in app_rows:
            try:
                parsed = _parse_purpose(r.purpose)
                phase1 = _build_phase1(r, parsed)
                params = {
                    "id":           r.id,
                    "consumer_id":  r.client_id,
                    "product_type": r.product_type,
                    "amount":       r.amount,
                    "term":         r.preferred_term_months,
                    "max_rate":     r.max_rate,
                    "deadline":     r.decision_deadline,
                    "status":       r.status,
                    "created_at":   r.created_at,
                    "phase1":       json.dumps(phase1),
                }
                # SAVEPOINT per row: ingest_app_request returns a plain uuid
                # (not a row with an `is_new` column, so RETURNING doesn't
                # apply here — it isn't DML). Isolating each row behind its
                # own nested transaction means a single bad/unexpected row
                # gets rolled back to the savepoint and recorded as failed,
                # instead of aborting the whole outer transaction and
                # silently failing every other row in the batch too.
                with conn.begin_nested():
                    existed = conn.execute(
                        text("SELECT 1 FROM marketplace.request WHERE id = :id"),
                        {"id": r.id},
                    ).fetchone() is not None
                    conn.execute(text(_INGEST_SQL), params)
                synced += 1
                # Only fire webhook for genuinely new requests (not re-syncs)
                if not existed:
                    new_requests.append({
                        "request_id": r.id,
                        "product_type": r.product_type,
                        "amount": float(r.amount or 0),
                        "term_months": r.preferred_term_months,
                    })
            except Exception as e:
                failed += 1
                if len(errors) < 10:
                    errors.append(f"{r.id}: {e}")

    # Dispatch request.new webhook to all institutions with matching product config
    # Fire-and-forget per new request — don't block the sync response
    if new_requests:
        with service_session() as conn:
            institution_rows = conn.execute(
                text("""
                    SELECT DISTINCT i.id AS institution_id, pc.product_type
                    FROM institution.product_config pc
                    JOIN institution.institution i ON i.id = pc.institution_id
                    WHERE pc.is_active = true
                      AND pc.product_type = ANY(CAST(:pts AS text[]))
                """),
                {"pts": list({r["product_type"] for r in new_requests})},
            ).fetchall()

        for inst_row in institution_rows:
            matching = [r for r in new_requests if r["product_type"] == inst_row.product_type]
            for req in matching:
                asyncio.create_task(dispatch_event(
                    str(inst_row.institution_id),
                    "request.new",
                    {**req, "source": "marketplace_sync"},
                ))

    return {"pulled": len(app_rows), "synced": synced, "failed": failed, "errors": errors}
