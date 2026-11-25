# =============================================================================
# ficium-portal-api — Booking hand-off (core banking)
#
# A deal ends when the loan is BOOKED in the bank's core banking system. This
# module records that hand-off — core reference, booking date, disbursed
# amount — from either channel:
#
#   Portal  /pipelines/{id}/booking          officer types it in   (source 'portal')
#   API     /v1/pipeline/{id}/booking        the bank's core posts it (source 'api')
#
# All rules live in marketplace.record_loan_booking() (db/025, fee models
# db/072) so both channels behave identically and the booking + commission
# event are written in one transaction. This module only maps its BOOKING_*
# errors to HTTP and runs the side effects (audit, notification, webhook).
#
# Fee inputs (db/072): what Ficium charges depends on the commission rule's
# model. A regular plan's fee needs the committed monthly amount, a term
# deposit's needs the term. fee_inputs() tells the caller which of these the
# rule in force needs, without revealing the rate.
#
# Closure (db/072): the bank reports the account closed (plan stopped, loan
# cancelled or repaid early). Inside the rule's clawback window Ficium's fee is
# credited back, in full or the unearned share, by
# marketplace.record_booking_closure(). A closed booking cannot be corrected.
# =============================================================================
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .webhooks import dispatch_event

log = structlog.get_logger()

MODULE_KEY = "inst:booking"


class BookingInput(BaseModel):
    core_reference:   str = Field(
        ..., min_length=1, max_length=64,
        description="Loan account / contract number in the core banking system.",
    )
    booked_on:        date
    disbursed_amount: Decimal = Field(..., gt=0, max_digits=18, decimal_places=2)
    disbursed_on:     date | None = None
    monthly_amount:   Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=2,
        description="Regular contribution the client committed to (savings and investment plans).",
    )
    term_months:      int | None = Field(
        default=None, ge=1, le=600,
        description="Term of the booked product in months, when it differs from the accepted deal.",
    )
    notes:            str | None = Field(default=None, max_length=1000)


class ClosureInput(BaseModel):
    closed_on: date
    reason:    str = Field(..., min_length=5, max_length=500,
                           description="Why the account ended, e.g. 'client stopped the plan'.")


# code -> (HTTP status, message shown to the officer / API client)
_ERRORS: dict[str, tuple[int, str]] = {
    "BOOKING_DEAL_NOT_FOUND": (404, "Deal not found."),
    "BOOKING_NOT_FOUND": (404, "No booking has been recorded for this deal yet."),
    "BOOKING_DEAL_NOT_COMPLETE": (
        409, "Complete every processing stage before recording the booking."),
    "BOOKING_ALREADY_RECORDED": (
        409, "A booking is already recorded for this deal. Correct it instead."),
    "BOOKING_REFERENCE_IN_USE": (
        409, "That core banking reference is already used by another deal."),
    "BOOKING_REFERENCE_INVALID": (
        422, "Enter the core banking reference (1 to 64 characters)."),
    "BOOKING_AMOUNT_INVALID": (422, "The disbursed amount must be greater than zero."),
    "BOOKING_AMOUNT_EXCEEDS_DEAL": (
        422, "The disbursed amount can't be more than the accepted deal amount."),
    "BOOKING_DATE_INVALID": (422, "The booking date can't be in the future."),
    "BOOKING_DATE_BEFORE_DEAL": (422, "The booking date can't be before the deal started."),
    "BOOKING_DISBURSED_DATE_INVALID": (
        422, "The disbursement date must be between the deal start and today."),
    "BOOKING_SOURCE_INVALID": (422, "Invalid booking source."),
    "BOOKING_MONTHLY_INVALID": (422, "The monthly amount can't be negative."),
    "BOOKING_TERM_INVALID": (422, "The term must be between 1 and 600 months."),
    "FEE_INPUT_MISSING:monthly_amount": (
        422, "Enter the monthly amount the client committed to (0 if none)."),
    "FEE_INPUT_MISSING:term_months": (422, "Enter the term in months."),
    "BOOKING_FEE_CURRENCY_MISMATCH": (
        409, "This deal's currency doesn't match Ficium's commission schedule. Contact Ficium."),
    "BOOKING_NO_COMMISSION_RULE": (
        409, "No Ficium commission schedule applies to this deal. Contact Ficium."),
    "BOOKING_CLOSED": (409, "This booking is closed and can no longer be corrected."),
    "BOOKING_ALREADY_CLOSED": (409, "This booking is already recorded as closed."),
    "BOOKING_CLOSURE_DATE_INVALID": (
        422, "The closing date must be between the booking date and today."),
    "BOOKING_CLOSURE_REASON_INVALID": (422, "Give a reason of 5 to 500 characters."),
}


def _booking_error(exc: DBAPIError) -> HTTPException | None:
    msg = str(getattr(exc, "orig", exc))
    for code, (status, detail) in _ERRORS.items():
        if code in msg:
            return HTTPException(status_code=status, detail=detail)
    return None


def has_booking_permission(modules: list[str] | None, user_role: str | None) -> bool:
    if user_role in ("super_admin", "institution_super_admin"):
        return True
    mods = modules or []
    return "*" in mods or MODULE_KEY in mods


def get_booking(conn: Session, institution_id: str, pipeline_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("""
            SELECT b.pipeline_id, b.core_reference, b.booked_on, b.disbursed_amount,
                   b.disbursed_on, b.monthly_amount, b.term_months, b.notes, b.source,
                   b.corrected_count, b.closed_on, b.closure_reason, b.closed_source,
                   b.created_at, b.updated_at,
                   portal_admin.get_user_display_name(b.recorded_by) AS recorded_by_name
              FROM marketplace.loan_booking b
             WHERE b.pipeline_id = CAST(:pid AS uuid)
               AND b.institution_id = CAST(:iid AS uuid)
        """),
        {"pid": pipeline_id, "iid": institution_id},
    ).mappings().first()
    return dict(row) if row else None


def fee_inputs(conn: Session, institution_id: str, pipeline_id: str) -> dict[str, bool] | None:
    """Which optional booking fields this deal's commission needs.

    {"monthly_amount": bool, "term_months": bool}, or None if the deal isn't this bank's.
    """
    out = conn.execute(
        text("SELECT marketplace.booking_fee_inputs("
             "CAST(:pid AS uuid), CAST(:iid AS uuid))"),
        {"pid": pipeline_id, "iid": institution_id},
    ).scalar()
    return dict(out) if out is not None else None


def record_booking(
    conn: Session,
    *,
    institution_id: str,
    pipeline_id:    str,
    body:           BookingInput,
    source:         str,
    actor_id:       str | None,
    actor_email:    str | None,
    api_key_id:     str | None,
    correction:     bool,
    background:     BackgroundTasks,
) -> dict[str, Any]:
    """Record (or correct) the booking, audit + notify in the same transaction,
    commit, then queue the webhook to run after the response."""
    try:
        result = conn.execute(
            text("""
                SELECT marketplace.record_loan_booking(
                    CAST(:pid AS uuid), CAST(:iid AS uuid), :ref, :booked_on, :amount,
                    :disbursed_on, :notes, :source, CAST(:actor AS uuid),
                    CAST(:key AS uuid), :correction,
                    CAST(:monthly AS numeric), CAST(:term AS integer)
                ) AS r
            """),
            {
                "pid": pipeline_id, "iid": institution_id, "ref": body.core_reference,
                "booked_on": body.booked_on, "amount": body.disbursed_amount,
                "disbursed_on": body.disbursed_on, "notes": body.notes, "source": source,
                "actor": actor_id, "key": api_key_id, "correction": correction,
                "monthly": body.monthly_amount, "term": body.term_months,
            },
        ).scalar_one()
    except DBAPIError as exc:
        conn.rollback()
        mapped = _booking_error(exc)
        if mapped:
            raise mapped from exc
        raise

    booking = result["booking"]
    action = "deal.booking_corrected" if correction else "deal.booked"

    # Notification + audit in the same transaction as the booking.
    conn.execute(
        text("""
            INSERT INTO public.portal_notifications
                (institution_id, kind, title, body, link, metadata)
            VALUES (CAST(:iid AS uuid), :kind, :title, :body, :link, CAST(:meta AS jsonb))
        """),
        {
            "iid": institution_id,
            "kind": "deal_booked",
            "title": "Booking corrected" if correction else "Deal booked",
            "body": f"Core banking reference {booking['core_reference']}.",
            "link": f"/deals/{pipeline_id}",
            "meta": json.dumps({"pipeline_id": pipeline_id, "source": source}),
        },
    )
    conn.execute(
        text("""
            INSERT INTO audit.event
                (id, occurred_at, actor_id, actor_type, actor_email, institution_id,
                 action, resource_type, resource_id, outcome, metadata)
            VALUES
                (gen_random_uuid(), now(), CAST(:actor AS uuid), :actor_type, :email,
                 CAST(:iid AS uuid), :action, 'loan_booking', CAST(:pid AS uuid), 'success',
                 CAST(:meta AS jsonb))
        """),
        {
            "actor": actor_id, "actor_type": "member" if source == "portal" else "api",
            "email": actor_email, "iid": institution_id, "action": action, "pid": pipeline_id,
            "meta": json.dumps({
                "source": source, "api_key_id": api_key_id,
                "booking": booking, "previous": result.get("previous"),
                "commission_status": result.get("commission_status"),
            }, default=str),
        },
    )
    conn.commit()

    background.add_task(dispatch_event, institution_id, action, {
        "loan_id":          pipeline_id,
        "core_reference":   booking["core_reference"],
        "booked_on":        booking["booked_on"],
        "disbursed_amount": booking["disbursed_amount"],
        "disbursed_on":     booking["disbursed_on"],
        "monthly_amount":   booking.get("monthly_amount"),
        "term_months":      booking.get("term_months"),
        "source":           source,
    })
    log.info("deal_booking_recorded", pipeline_id=pipeline_id, source=source, correction=correction)

    return booking


def record_closure(
    conn: Session,
    *,
    institution_id: str,
    pipeline_id:    str,
    body:           ClosureInput,
    source:         str,
    actor_id:       str | None,
    actor_email:    str | None,
    api_key_id:     str | None,
    background:     BackgroundTasks,
) -> dict[str, Any]:
    """Record that the booked account ended. Any clawback is Ficium-side only:
    it goes to the audit trail, not back to the bank's caller."""
    try:
        result = conn.execute(
            text("""
                SELECT marketplace.record_booking_closure(
                    CAST(:pid AS uuid), CAST(:iid AS uuid), :closed_on, :reason, :source,
                    CAST(:actor AS uuid), CAST(:key AS uuid)
                ) AS r
            """),
            {
                "pid": pipeline_id, "iid": institution_id, "closed_on": body.closed_on,
                "reason": body.reason, "source": source, "actor": actor_id, "key": api_key_id,
            },
        ).scalar_one()
    except DBAPIError as exc:
        conn.rollback()
        mapped = _booking_error(exc)
        if mapped:
            raise mapped from exc
        raise

    booking = result["booking"]
    conn.execute(
        text("""
            INSERT INTO public.portal_notifications
                (institution_id, kind, title, body, link, metadata)
            VALUES (CAST(:iid AS uuid), 'booking_closed', 'Booking closed', :body, :link,
                    CAST(:meta AS jsonb))
        """),
        {
            "iid": institution_id,
            "body": (f"Core banking reference {booking['core_reference']} "
                     f"closed on {body.closed_on}."),
            "link": f"/deals/{pipeline_id}",
            "meta": json.dumps({"pipeline_id": pipeline_id, "source": source}),
        },
    )
    conn.execute(
        text("""
            INSERT INTO audit.event
                (id, occurred_at, actor_id, actor_type, actor_email, institution_id,
                 action, resource_type, resource_id, outcome, metadata)
            VALUES
                (gen_random_uuid(), now(), CAST(:actor AS uuid), :actor_type, :email,
                 CAST(:iid AS uuid), 'deal.booking_closed', 'loan_booking', CAST(:pid AS uuid),
                 'success', CAST(:meta AS jsonb))
        """),
        {
            "actor": actor_id, "actor_type": "member" if source == "portal" else "api",
            "email": actor_email, "iid": institution_id, "pid": pipeline_id,
            "meta": json.dumps({
                "source": source, "api_key_id": api_key_id, "closed_on": body.closed_on,
                "reason": body.reason, "clawback": result.get("clawback"),
            }, default=str),
        },
    )
    conn.commit()

    background.add_task(dispatch_event, institution_id, "deal.booking_closed", {
        "loan_id":        pipeline_id,
        "core_reference": booking["core_reference"],
        "closed_on":      booking["closed_on"],
        "reason":         booking["closure_reason"],
        "source":         source,
    })
    log.info("deal_booking_closed", pipeline_id=pipeline_id, source=source,
             clawback=bool(result.get("clawback")))
    return booking
