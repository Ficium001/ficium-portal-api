# =============================================================================
# ficium-portal-api — /v1/ Public API (machine-to-machine)
#
# Versioned, stable API surface for institution system integrations.
# Accepts EITHER a portal JWT (human operators) or an institution API key.
#
# Scope requirements:
#   GET  /v1/requests          → marketplace:read
#   POST /v1/bids              → bids:write
#   GET  /v1/bids              → bids:read
#   PUT  /v1/pipeline/.../advance → pipeline:write
#   GET  /v1/analytics/summary → analytics:read
#
# Response shape is stable — breaking changes increment to /v2/.
# =============================================================================

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...core.webhooks import dispatch_event
from ...deps import api_or_jwt_claims, require_scope, service_session
from ...core.db import service_session as _svc
from ...core.ratelimit import limiter

import asyncio

router = APIRouter(prefix="/v1", tags=["v1-public-api"])


def _institution_id_from_claims(claims: dict[str, Any]) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="Cannot determine institution from token.")
    return str(iid)


# ---------------------------------------------------------------------------
# GET /v1/requests — Browse marketplace requests eligible for this institution
# ---------------------------------------------------------------------------

@router.get("/requests", dependencies=[Depends(require_scope("marketplace:read"))])
def list_requests(
    claims: dict[str, Any] = Depends(api_or_jwt_claims),
    product_type: str | None = Query(default=None),
    amount_min: float | None = Query(default=None),
    amount_max: float | None = Query(default=None),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """
    Browse open borrower requests in the marketplace.
    Filtered to products the institution is configured and permitted to bid on.
    """
    iid = _institution_id_from_claims(claims)

    with _svc() as conn:
        params: dict[str, Any] = {"iid": iid, "lim": limit, "off": offset}
        filters = [
            "r.status = 'open'",
            "r.bid_window_closes_at > now()",
            # Only return requests for product types this institution has active config for
            """EXISTS (
                SELECT 1 FROM institution.product_config pc
                WHERE pc.institution_id = CAST(:iid AS uuid)
                  AND pc.product_type   = r.product_type
                  AND pc.is_active      = true
            )""",
        ]

        if product_type:
            filters.append("r.product_type = :product_type")
            params["product_type"] = product_type
        if amount_min is not None:
            filters.append("r.amount_requested >= :amount_min")
            params["amount_min"] = amount_min
        if amount_max is not None:
            filters.append("r.amount_requested <= :amount_max")
            params["amount_max"] = amount_max

        where = " AND ".join(filters)

        rows = conn.execute(
            text(f"""
                SELECT
                    r.id,
                    r.product_type,
                    r.amount_requested,
                    r.term_months,
                    r.purpose,
                    r.ficium_risk_tier,
                    r.ficium_score,
                    r.bid_window_closes_at,
                    r.created_at,
                    -- Has this institution already bid?
                    EXISTS (
                        SELECT 1 FROM marketplace.bid b
                        WHERE b.request_id    = r.id
                          AND b.institution_id = CAST(:iid AS uuid)
                          AND b.status NOT IN ('withdrawn')
                    ) AS already_bid
                FROM marketplace.request r
                WHERE {where}
                ORDER BY r.created_at DESC
                LIMIT :lim OFFSET :off
            """),
            params,
        ).fetchall()

        total = conn.execute(
            text(f"SELECT COUNT(*) FROM marketplace.request r WHERE {where}"),
            params,
        ).scalar()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "requests": [dict(r._mapping) for r in rows],
    }


# ---------------------------------------------------------------------------
# GET /v1/bids — List institution's own bids
# ---------------------------------------------------------------------------

@router.get("/bids", dependencies=[Depends(require_scope("bids:read"))])
def list_bids(
    claims: dict[str, Any] = Depends(api_or_jwt_claims),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    iid = _institution_id_from_claims(claims)

    with _svc() as conn:
        params: dict[str, Any] = {"iid": iid, "lim": limit, "off": offset}
        filters = ["b.institution_id = CAST(:iid AS uuid)"]
        if status:
            filters.append("b.status = :status")
            params["status"] = status

        where = " AND ".join(filters)

        rows = conn.execute(
            text(f"""
                SELECT
                    b.id, b.request_id, b.status,
                    b.rate, b.rate_type, b.amount_offered, b.term_months,
                    b.rate_valid_days, b.conditions, b.submitted_at, b.expires_at,
                    r.product_type, r.amount_requested, r.ficium_risk_tier
                FROM marketplace.bid b
                JOIN marketplace.request r ON r.id = b.request_id
                WHERE {where}
                ORDER BY b.submitted_at DESC
                LIMIT :lim OFFSET :off
            """),
            params,
        ).fetchall()

        total = conn.execute(
            text(f"SELECT COUNT(*) FROM marketplace.bid b WHERE {where}"),
            params,
        ).scalar()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "bids": [dict(r._mapping) for r in rows],
    }


# ---------------------------------------------------------------------------
# POST /v1/bids — Submit a bid programmatically from institution LOS
# ---------------------------------------------------------------------------

class BidSubmitRequest(BaseModel):
    request_id: str
    rate: float = Field(..., gt=0, lt=100)
    rate_type: str = Field(..., pattern="^(fixed|variable|base_plus)$")
    amount_offered: float = Field(..., gt=0)
    term_months: int = Field(..., gt=0)
    rate_valid_days: int = Field(default=30, ge=1, le=365)
    conditions: str | None = Field(default=None, max_length=2000)
    fee_structure: dict | None = None


@router.post("/bids", status_code=201, dependencies=[Depends(require_scope("bids:write"))])
@limiter.limit("60/minute")  # tighter than global default — bid writes are the key abuse vector
def submit_bid(
    request: Request,  # required by slowapi's per-route limiter
    body: Annotated[BidSubmitRequest, Body()],
    claims: dict[str, Any] = Depends(api_or_jwt_claims),
) -> dict:
    """
    Submit a bid on a marketplace request from an institution system.
    Validates the request is open, bid window is active, and no duplicate bid exists.
    """
    iid = _institution_id_from_claims(claims)

    with _svc() as conn:
        # Validate request is open and biddable
        req = conn.execute(
            text("""
                SELECT id, status, bid_window_closes_at, product_type
                FROM marketplace.request
                WHERE id = CAST(:rid AS uuid)
            """),
            {"rid": body.request_id},
        ).fetchone()

        if req is None:
            raise HTTPException(status_code=404, detail="Request not found.")
        if req.status != "open":
            raise HTTPException(status_code=409, detail=f"Request is {req.status}, not open.")
        
        from datetime import datetime, timezone
        if req.bid_window_closes_at and req.bid_window_closes_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=409, detail="Bid window has closed.")

        # Check for duplicate active bid
        dup = conn.execute(
            text("""
                SELECT id FROM marketplace.bid
                WHERE request_id     = CAST(:rid AS uuid)
                  AND institution_id = CAST(:iid AS uuid)
                  AND status NOT IN ('withdrawn', 'rejected')
            """),
            {"rid": body.request_id, "iid": iid},
        ).fetchone()
        if dup is not None:
            raise HTTPException(status_code=409, detail="A bid already exists for this request.")

        # Check compliance gate — institution must have active product_config
        config = conn.execute(
            text("""
                SELECT id FROM institution.product_config
                WHERE institution_id = CAST(:iid AS uuid)
                  AND product_type   = :pt
                  AND is_active      = true
            """),
            {"iid": iid, "pt": req.product_type},
        ).fetchone()
        if config is None:
            raise HTTPException(
                status_code=403,
                detail=f"Institution not configured to bid on {req.product_type} products.",
            )

        import json as _json
        row = conn.execute(
            text("""
                INSERT INTO marketplace.bid
                    (request_id, institution_id, rate, rate_type,
                     amount_offered, term_months, rate_valid_days,
                     conditions, fee_structure, status)
                VALUES
                    (CAST(:rid AS uuid), CAST(:iid AS uuid), :rate, :rate_type,
                     :amount, :term, :valid_days,
                     :conditions, CAST(:fee AS jsonb), 'submitted')
                RETURNING id, request_id, status, submitted_at, expires_at
            """),
            {
                "rid": body.request_id,
                "iid": iid,
                "rate": body.rate,
                "rate_type": body.rate_type,
                "amount": body.amount_offered,
                "term": body.term_months,
                "valid_days": body.rate_valid_days,
                "conditions": body.conditions,
                "fee": _json.dumps(body.fee_structure) if body.fee_structure else "null",
            },
        ).fetchone()
        conn.commit()
        result = dict(row._mapping)

    # Fire webhook to the institution (confirmation their system bid was received)
    asyncio.create_task(
        dispatch_event(iid, "bid.accepted", {
            "bid_id": str(result["id"]),
            "request_id": body.request_id,
            "status": "submitted",
            "source": "api",
        })
    )

    return result


# ---------------------------------------------------------------------------
# PUT /v1/pipeline/{loan_id}/stages/{stage_id}/advance
# ---------------------------------------------------------------------------

class StageAdvanceRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
    metadata: dict | None = None


@router.put(
    "/pipeline/{loan_id}/stages/{stage_id}/advance",
    dependencies=[Depends(require_scope("pipeline:write"))],
)
def advance_pipeline_stage(
    loan_id: str,
    stage_id: str,
    body: Annotated[StageAdvanceRequest, Body()],
    claims: dict[str, Any] = Depends(api_or_jwt_claims),
) -> dict:
    """
    Advance a pipeline stage from the institution's LOS.
    Stage must be the current active stage (in_progress) for this pipeline.
    """
    iid = _institution_id_from_claims(claims)

    with _svc() as conn:
        # Verify pipeline belongs to institution
        pipeline = conn.execute(
            text("""
                SELECT id, status FROM marketplace.loan_pipeline
                WHERE id = CAST(:lid AS uuid) AND institution_id = CAST(:iid AS uuid)
            """),
            {"lid": loan_id, "iid": iid},
        ).fetchone()
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Pipeline not found.")
        if pipeline.status in ("completed", "cancelled"):
            raise HTTPException(status_code=409, detail=f"Pipeline is {pipeline.status}.")

        # Verify stage belongs to this pipeline and is in_progress
        stage = conn.execute(
            text("""
                SELECT psi.id, psi.status, psi.position,
                       psd.stage_key, psd.borrower_label
                FROM marketplace.pipeline_stage_instance psi
                JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
                WHERE psi.id          = CAST(:sid AS uuid)
                  AND psi.pipeline_id = CAST(:lid AS uuid)
            """),
            {"sid": stage_id, "lid": loan_id},
        ).fetchone()
        if stage is None:
            raise HTTPException(status_code=404, detail="Stage not found in this pipeline.")
        if stage.status != "in_progress":
            raise HTTPException(status_code=409, detail=f"Stage is {stage.status}, not in_progress.")

        # Complete this stage
        conn.execute(
            text("""
                UPDATE marketplace.pipeline_stage_instance
                SET status       = 'completed',
                    completed_at = now()
                WHERE id = CAST(:sid AS uuid)
            """),
            {"sid": stage_id},
        )

        # Activate the next stage if it exists
        next_stage = conn.execute(
            text("""
                UPDATE marketplace.pipeline_stage_instance
                SET status     = 'in_progress',
                    started_at = now()
                WHERE pipeline_id = CAST(:lid AS uuid)
                  AND position    = :next_pos
                  AND status      = 'pending'
                RETURNING id, position
            """),
            {"lid": loan_id, "next_pos": stage.position + 1},
        ).fetchone()

        # If no next stage, complete the pipeline
        if next_stage is None:
            conn.execute(
                text("""
                    UPDATE marketplace.loan_pipeline
                    SET status = 'completed', completed_at = now()
                    WHERE id = CAST(:lid AS uuid)
                """),
                {"lid": loan_id},
            )
            pipeline_done = True
        else:
            pipeline_done = False

        conn.commit()

    # Fire webhook: pipeline.stage_changed
    asyncio.create_task(
        dispatch_event(iid, "pipeline.stage_changed", {
            "loan_id": loan_id,
            "stage_id": stage_id,
            "stage_key": stage.stage_key,
            "previous_status": "in_progress",
            "new_status": "completed",
            "pipeline_completed": pipeline_done,
            "note": body.note,
        })
    )

    return {
        "loan_id": loan_id,
        "stage_id": stage_id,
        "stage_key": stage.stage_key,
        "status": "completed",
        "pipeline_completed": pipeline_done,
        "next_stage_id": str(next_stage.id) if next_stage else None,
    }


# ---------------------------------------------------------------------------
# GET /v1/analytics/summary
# ---------------------------------------------------------------------------

@router.get("/analytics/summary", dependencies=[Depends(require_scope("analytics:read"))])
def analytics_summary(
    claims: dict[str, Any] = Depends(api_or_jwt_claims),
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Summarised bid/pipeline performance metrics for the institution."""
    iid = _institution_id_from_claims(claims)

    with _svc() as conn:
        bids = conn.execute(
            text("""
                SELECT
                    COUNT(*)                                        AS total_bids,
                    COUNT(*) FILTER (WHERE status = 'accepted')    AS accepted_bids,
                    COUNT(*) FILTER (WHERE status = 'rejected')    AS rejected_bids,
                    COUNT(*) FILTER (WHERE status = 'submitted')   AS pending_bids,
                    AVG(rate)                                       AS avg_rate,
                    AVG(amount_offered)                             AS avg_amount,
                    SUM(amount_offered) FILTER (WHERE status='accepted') AS total_accepted_value
                FROM marketplace.bid
                WHERE institution_id = CAST(:iid AS uuid)
                  AND submitted_at >= now() - CAST(:days || ' days' AS interval)
            """),
            {"iid": iid, "days": days},
        ).fetchone()

        pipelines = conn.execute(
            text("""
                SELECT
                    COUNT(*)                                           AS total_pipelines,
                    COUNT(*) FILTER (WHERE status = 'completed')      AS completed_pipelines,
                    COUNT(*) FILTER (WHERE status = 'in_progress')    AS active_pipelines,
                    AVG(deal_amount)                                   AS avg_deal_amount,
                    AVG(deal_rate)                                     AS avg_deal_rate,
                    AVG(EXTRACT(EPOCH FROM (completed_at - started_at))/86400)
                        FILTER (WHERE status = 'completed')           AS avg_completion_days
                FROM marketplace.loan_pipeline
                WHERE institution_id = CAST(:iid AS uuid)
                  AND started_at >= now() - CAST(:days || ' days' AS interval)
            """),
            {"iid": iid, "days": days},
        ).fetchone()

    bid_d = dict(bids._mapping)
    pip_d = dict(pipelines._mapping)

    # Round floats
    for k, v in bid_d.items():
        if v is not None and isinstance(v, float):
            bid_d[k] = round(v, 4)
    for k, v in pip_d.items():
        if v is not None and isinstance(v, float):
            pip_d[k] = round(v, 2)

    acceptance_rate = (
        round(bid_d["accepted_bids"] / bid_d["total_bids"] * 100, 1)
        if bid_d["total_bids"]
        else 0.0
    )

    return {
        "period_days": days,
        "bids": {**bid_d, "acceptance_rate_pct": acceptance_rate},
        "pipelines": pip_d,
    }
