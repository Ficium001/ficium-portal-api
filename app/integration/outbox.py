"""Write events to integration.outbox inside the caller's transaction."""

from __future__ import annotations

import json
from typing import Any

import ficium_contract as fc
from sqlalchemy import text
from sqlalchemy.orm import Session

SOURCE = "institution"


def enqueue(
    session: Session, event_type: str, aggregate_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Enqueue one event in the caller's transaction and validate it.

    Raises ficium_contract.ContractError if the event breaks the contract;
    the caller's transaction then rolls back, so an invalid event is never
    stored and the business change that caused it is not committed either.
    """
    envelope: dict[str, Any] = session.execute(
        text("SELECT integration.enqueue(:t, :a, CAST(:d AS jsonb), :s)"),
        {"t": event_type, "a": aggregate_id, "d": json.dumps(data), "s": SOURCE},
    ).scalar_one()
    fc.validate_event(envelope)
    return envelope
