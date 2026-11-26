"""The anonymous request object banks see: one shape for rules, the bank's own
decision engine, and the bank API (GET /v1/requests).

Built only from what the borrower released at Phase 1 (marketplace.request and its
allowlisted metadata, db/021 + ingest_request). Never included: the contract
borrower id (consumer_id / consumer_ref, stable across a borrower's requests, so
it would let a bank link them), the employer, or anything identifying.

Values arrive loosely typed (the borrower app stores every answer as text), so
numbers and booleans are coerced here, once. A value that cannot be read is left
out, and a rule condition on it is then UNKNOWN (never matches; see
autobid_rules.evaluate).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .autobid_rules import FIELDS, RISK_TIERS

# Columns of the projection; the SQL below and build_facts() must agree.
REQUEST_SQL = """
    SELECT r.id,
           coalesce(r.params ->> 'app_product_type',
                    integration.app_type_for_product(p.code)) AS product_type,
           r.amount, r.term_months, r.currency, r.status,
           r.bid_window_closes_at, r.created_at, r.allocation_mode,
           r.ficium_risk_tier, r.ficium_score, r.params, r.metadata, r.product_id
      FROM marketplace.request r
      JOIN catalog.product p ON p.id = r.product_id
"""


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if s.endswith("%"):
            s = s[:-1]
        try:
            f = float(s)
        except ValueError:
            return None
        return int(f) if f.is_integer() else f
    return None


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def _str(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()[:80]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def build_facts(row: dict[str, Any]) -> dict[str, Any]:
    """Request row (REQUEST_SQL) -> flat facts keyed by the rule vocabulary."""
    meta = row.get("metadata") or {}
    params = row.get("params") or {}
    raw: dict[str, Any] = {
        "product": row.get("product_type"),
        "amount": row.get("amount"),
        "term_months": row.get("term_months"),
        "currency": row.get("currency"),
        "risk_tier": meta.get("risk_tier") or row.get("ficium_risk_tier"),
        "ficium_score": row.get("ficium_score")
        if row.get("ficium_score") is not None
        else meta.get("risk_score"),
        "collateral_type": params.get("collateral_type"),
        "ltv_pct": params.get("ltv_pct"),
    }
    for key in FIELDS:
        if key not in raw:
            raw[key] = meta.get(key)

    facts: dict[str, Any] = {}
    for key, kind in FIELDS.items():
        v = raw.get(key)
        if kind == "num":
            out: Any = _num(v)
            if key == "term_months" and out is not None:
                out = int(out)
        elif kind == "bool":
            out = _bool(v)
        elif kind == "tier":
            s = _str(v)
            out = s.upper() if s and s.upper() in RISK_TIERS else None
        else:
            out = _str(v)
        if out is not None:
            facts[key] = out
    return facts


def request_object(row: dict[str, Any]) -> dict[str, Any]:
    """The JSON a bank receives for one request (decision engine and /v1/requests)."""
    closes = row.get("bid_window_closes_at")
    created = row.get("created_at")
    return {
        "request_id": str(row["id"]),
        "product_type": row.get("product_type"),
        "status": "open" if row.get("status") == "bidding" else row.get("status"),
        "bid_window_closes_at": closes.isoformat() if closes else None,
        "created_at": created.isoformat() if created else None,
        "allocation_mode": row.get("allocation_mode"),
        "facts": build_facts(row),
    }


def load_request(conn: Session, request_id: str) -> dict[str, Any] | None:
    row = (
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        conn.execute(text(REQUEST_SQL + " WHERE r.id = CAST(:rid AS uuid)"), {"rid": request_id})
        .mappings()
        .first()
    )
    return dict(row) if row else None
