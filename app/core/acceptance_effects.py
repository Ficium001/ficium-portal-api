"""Side effects after a bid is accepted (contract acceptance call, acceptance.py)."""
from __future__ import annotations

from ..api.notifications import _write_notification
from .db import service_session


async def after_acceptance(result: dict, request_id: str, bid_id: str) -> None:
    """Notify the winning institution and fire its webhooks.

    Field names follow what marketplace.accept_bid() actually returns (pipeline_id,
    amount_offered, rate, term_months). The previous inline code read loan_id / deal_amount /
    deal_rate / deal_term_months, which the function never returns, so every bid.accepted webhook
    carried null deal terms.
    """
    import asyncio as _asyncio

    from .webhooks import dispatch_event as _dispatch

    winning_institution = result.get("institution_id")
    if not winning_institution:
        return
    product_label = result.get("product_label", "loan")
    pipeline_id = str(result["pipeline_id"]) if result.get("pipeline_id") else None

    with service_session() as notif_conn:
        _write_notification(
            notif_conn,
            str(winning_institution),
            kind="bid_accepted",
            title="Your bid was accepted!",
            body=(f"A borrower accepted your {product_label} offer. "
                  "Their identity has been revealed."),
            link="/bids",
            metadata={"request_id": request_id, "bid_id": bid_id},
        )
        notif_conn.commit()

    _asyncio.create_task(
        _dispatch(
            str(winning_institution),
            "bid.accepted",
            {
                "bid_id": bid_id,
                "request_id": request_id,
                "pipeline_id": pipeline_id,
                "loan_id": pipeline_id,  # kept for existing subscribers; same value as pipeline_id
                "deal_amount": result.get("amount_offered"),
                "deal_rate": result.get("rate"),
                "deal_term_months": result.get("term_months"),
                "institution_id": str(winning_institution),
            },
        )
    )
    # Same moment, separate signal: a bank's system may subscribe only to this to know when to pull
    # borrower PII through the pipeline API. No PII in the webhook itself.
    _asyncio.create_task(
        _dispatch(
            str(winning_institution),
            "identity.revealed",
            {
                "bid_id": bid_id,
                "request_id": request_id,
                "pipeline_id": pipeline_id,
                "loan_id": pipeline_id,
                "pii_available": True,
            },
        )
    )
