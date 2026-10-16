# =============================================================================
# ficium-portal-api — Deal booking (portal channel)
#
#   GET  /pipelines/{id}/booking   the booking, or 404 if not recorded yet  (inst:pipeline)
#   POST /pipelines/{id}/booking   record it                                 (inst:booking)
#   PUT  /pipelines/{id}/booking   correct it (audited with previous values) (inst:booking)
#
# The bank's own systems use /v1/pipeline/{id}/booking instead (API key,
# pipeline:write). Rules and the commission event: app/core/booking.py and
# marketplace.record_loan_booking() (db/025).
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.booking import BookingInput, get_booking, has_booking_permission, record_booking
from ..core.db import service_session
from ..deps import current_claims, require_module, tenant_conn

router = APIRouter(
    prefix="/pipelines",
    tags=["booking"],
    dependencies=[Depends(require_module("pipeline"))],
)


def _iid(claims: dict[str, Any]) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    return str(iid)


def _require_booking_permission(conn: Session, claims: dict[str, Any]) -> None:
    # Live group permissions (not the JWT snapshot), so a newly granted module
    # works without signing out and back in.
    row = conn.execute(text("SELECT modules FROM institution.current_member_ctx_v2()")).first()
    modules = list(row.modules) if row and row.modules else []
    if not has_booking_permission(modules, claims.get("user_role")):
        raise HTTPException(status_code=403, detail="Requires 'inst:booking'.")


@router.get("/{pipeline_id}/booking")
def read_booking(
    pipeline_id: str,
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    booking = get_booking(conn, _iid(claims), pipeline_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="No booking recorded yet.")
    return booking


def _write(
    pipeline_id: str, body: BookingInput, conn: Session, claims: dict[str, Any],
    correction: bool, background: BackgroundTasks,
) -> dict[str, Any]:
    iid = _iid(claims)
    _require_booking_permission(conn, claims)
    with service_session() as svc:
        return record_booking(
            svc, institution_id=iid, pipeline_id=pipeline_id, body=body, source="portal",
            actor_id=claims.get("sub"), actor_email=claims.get("email"), api_key_id=None,
            correction=correction, background=background,
        )


@router.post("/{pipeline_id}/booking", status_code=201)
def create_booking(
    pipeline_id: str,
    background: BackgroundTasks,
    body: BookingInput = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    return _write(pipeline_id, body, conn, claims, correction=False, background=background)


@router.put("/{pipeline_id}/booking")
def correct_booking(
    pipeline_id: str,
    background: BackgroundTasks,
    body: BookingInput = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    return _write(pipeline_id, body, conn, claims, correction=True, background=background)
