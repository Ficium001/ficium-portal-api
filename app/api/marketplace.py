from __future__ import annotations

import hmac
import json
from datetime import UTC

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.db import AppDatabaseUnavailable, app_service_session, service_session
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

def _rows(result) -> list[dict]:  return [dict(r._mapping) for r in result.fetchall()]
def _row(r)       -> dict:        return dict(r._mapping)

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
               (SELECT COUNT(*) FROM marketplace.bid b
                WHERE b.request_id = r.id
                  AND b.status NOT IN ('withdrawn','rejected')) AS bid_count
        FROM marketplace.request r
        JOIN catalog.product        p  ON p.id  = r.product_id
        JOIN catalog.product_family pf ON pf.id = p.family_id
        WHERE r.status IN ('open', 'bidding')
    """
    params: dict = {}
    if product_type:
        sql += " AND p.code = :pt"
        params["pt"] = product_type
    sql += " ORDER BY r.bid_window_closes_at ASC"
    rows = conn.execute(text(sql), params).fetchall()

    if not rows:
        try:
            with app_service_session() as app_conn:
                result = app_conn.execute(text("""
                    SELECT r.id, r.product_type, r.status,
                           r.amount::NUMERIC AS amount, 'MUR' AS currency,
                           r.preferred_term_months AS term_months,
                           COALESCE(r.decision_deadline, now() + interval '72 hours') AS bid_window_closes_at,
                           r.created_at,
                           LEFT(md5(r.client_id::text || ':ficium-anon-v1:'), 8) AS consumer_ref,
                           0 AS bid_count
                    FROM public.requests r WHERE r.status = 'open'
                    ORDER BY r.created_at DESC
                """))
                return _rows(result)
        except AppDatabaseUnavailable:
            return []
    return [_row(r) for r in rows]

# ── GET /marketplace/my-bids ──────────────────────────────────────────────────
@router.get("/my-bids")
async def list_my_bids(
    status: str | None = Query(default=None),
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    if claims.get("user_role") in ("admin", "super_admin"):
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
    with service_session() as conn:
        result = conn.execute(text("SELECT marketplace.close_expired_windows() AS closed")).fetchone()
    closed = result.closed if result else 0
    return {"closed": closed, "message": f"Closed {closed} expired bid window(s)."}

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
    with service_session() as conn:
        for r in app_rows:
            try:
                parsed = _parse_purpose(r.purpose)
                phase1 = _build_phase1(r, parsed)
                conn.execute(text(_INGEST_SQL), {
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
                })
                synced += 1
            except Exception as e:
                failed += 1
                if len(errors) < 10:
                    errors.append(f"{r.id}: {e}")
    return {"pulled": len(app_rows), "synced": synced, "failed": failed, "errors": errors}
