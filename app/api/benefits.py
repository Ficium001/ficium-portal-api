# =============================================================================
# ficium-portal-api — Benefits router
#
# Module gate:  inst:benefits
# Write model:  every change → governance.submit (db/040, 042). The bank's
#               Setup > Controls switch decides: four-eyes on → queued for a
#               checker; off → applied at once, recorded as one-person control.
#               (Until 2026-10-19 only "guaranteed" benefits had four-eyes;
#               the rest, and every deactivation, were direct writes.)
# Read model:   direct, institution-scoped via RLS
#
# Endpoints:
#   GET  /benefits/categories          — Ficium-seeded categories (reference)
#   GET  /benefits                     — institution's active benefits
#   POST /benefits                     — create  (benefit.create)
#   PUT  /benefits/{id}                — update  (benefit.update)
#   DELETE /benefits/{id}              — deactivate (benefit.delete)
#   Each returns {pending, status, action_id}.
# =============================================================================

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/benefits", tags=["benefits"])

_MODULE = "inst:benefits"


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result.fetchall()]


def _require_module(claims: dict) -> None:
    """Enforce inst:benefits permission at the application layer."""
    perms: list[str] = claims.get("module_permissions", [])
    if "*" not in perms and _MODULE not in perms:
        raise HTTPException(status_code=403, detail="Module access denied: inst:benefits")


def _institution_id(claims: dict) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="No institution context.")
    return iid


# ─── GET /benefits/categories ─────────────────────────────────────────────────

@router.get("/categories")
async def list_benefit_categories(
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> list[dict]:
    """Ficium-seeded benefit category reference data."""
    _require_module(claims)
    result = conn.execute(
        text("SELECT * FROM institution.benefit_cat ORDER BY sort_order")
    )
    return _rows(result)


# ─── Structured value (db/028) ─────────────────────────────────────────────────
# A benefit's value is a number with a unit, so the borrower side can price it.
# Which units a category accepts, and whether a value is required:
#   rate_discount  pct_pa (rate points per year)             required
#   fee_waiver     pct (of the fee) or mur                   required
#   reward         mur (cashback / reward)                   required
#   insurance      mur (cover amount)                        optional
#   relationship_mgr, fast_track, bundled_product, other     no value
# value_display is written here from the number, never typed, so every bid
# shows benefits the same way.
_UNITS: dict[str, tuple[set[str], bool]] = {
    "rate_discount": ({"pct_pa"}, True),
    "fee_waiver":    ({"pct", "mur"}, True),
    "reward":        ({"mur"}, True),
    "insurance":     ({"mur"}, False),
}
_MAX = {"pct_pa": Decimal("5"), "pct": Decimal("100"), "mur": Decimal("10000000")}


def _display(cat: str, amount: Decimal | None, unit: str | None) -> str | None:
    if amount is None or unit is None:
        return None
    n = f"{amount.normalize():f}"
    if unit == "pct_pa":
        return f"{n}% p.a. off the rate"
    if unit == "pct":
        return f"{n}% of the fee waived"
    mur = f"MUR {amount:,.0f}"
    return {"fee_waiver": f"{mur} fee waived", "reward": f"{mur} cashback",
            "insurance": f"Cover up to {mur}"}.get(cat, mur)


def _normalise(conn: Session, body: dict, existing: dict | None = None) -> dict:
    """Merge body over the existing record, validate, and derive value_display."""
    rec = {**(existing or {}), **{k: v for k, v in body.items() if k in _FIELDS}}
    for k in ("product_id", "description", "conditions", "valid_from", "valid_until"):
        if rec.get(k) in ("", None):
            rec[k] = None
    title = (rec.get("title") or "").strip()
    if not 2 <= len(title) <= 80:
        raise HTTPException(status_code=422, detail="Title must be 2 to 80 characters.")
    rec["title"] = title
    if not rec.get("cat_id"):
        raise HTTPException(status_code=422, detail="Category is required.")
    cat = conn.execute(text("SELECT code FROM institution.benefit_cat WHERE id = :id"),
                       {"id": str(rec["cat_id"])}).scalar()
    if cat is None:
        raise HTTPException(status_code=422, detail="Unknown category.")

    units, required = _UNITS.get(cat, (set(), False))
    raw_amount, unit = rec.get("value_amount"), rec.get("value_unit") or None
    try:
        amount = Decimal(str(raw_amount)) if raw_amount not in (None, "") else None
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="Value must be a number.") from exc
    if not units:
        amount, unit = None, None
    elif amount is None:
        if required:
            raise HTTPException(status_code=422, detail="This kind of benefit needs a value.")
        unit = None
    else:
        if unit not in units:
            raise HTTPException(status_code=422, detail=f"Unit must be one of {sorted(units)}.")
        if not Decimal("0") < amount <= _MAX[unit]:
            raise HTTPException(status_code=422,
                                detail=f"Value must be more than 0 and at most {_MAX[unit]:f}.")
    rec["value_amount"], rec["value_unit"] = amount, unit
    rec["value_display"] = _display(cat, amount, unit)

    vf, vu = rec.get("valid_from"), rec.get("valid_until")
    if vf and vu and str(vu) < str(vf):
        raise HTTPException(status_code=422, detail="'Valid until' is before 'valid from'.")
    return rec


_FIELDS = {"product_id", "cat_id", "title", "description", "value_amount", "value_unit",
           "conditions", "valid_from", "valid_until", "is_active", "is_guaranteed"}


def _payload(rec: dict) -> str:
    return json.dumps({k: rec.get(k) for k in _FIELDS | {"value_display"}}, default=str)


def _submit(conn: Session, category: str, resource_id: str | None, payload: dict) -> dict:
    """Send a benefit change through the four-eyes choke point and report
    whether it was applied now (switch off) or queued for a checker."""
    row = conn.execute(
        text("""
            SELECT a.id, a.status, a.control
              FROM governance.action a
             WHERE a.id = institution.submit_for_approval(
                     :cat, 'benefit', CAST(:rid AS uuid), CAST(:payload AS jsonb))
        """),
        {"cat": category, "rid": resource_id, "payload": json.dumps(payload, default=str)},
    ).fetchone()
    conn.commit()
    if row is None:
        raise HTTPException(status_code=500, detail="Submission returned no action.")
    executed = row.control == "single" and row.status == "approved"
    return {
        "pending": not executed,
        "status": "executed" if executed else "pending_approval",
        "action_id": str(row.id),
    }


# ─── GET /benefits ─────────────────────────────────────────────────────────────

@router.get("")
async def list_benefits(
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> list[dict]:
    """Active benefits for the caller's institution, enriched with category info."""
    _require_module(claims)
    result = conn.execute(
        text("""
            SELECT
                b.*,
                bc.code       AS cat_code,
                bc.label      AS cat_label,
                bc.icon_key   AS cat_icon,
                p.code        AS product_code,
                p.label       AS product_label
            FROM institution.benefit b
            JOIN institution.benefit_cat bc ON bc.id = b.cat_id
            LEFT JOIN catalog.product      p  ON p.id  = b.product_id
            ORDER BY b.created_at DESC
        """)
    )
    return _rows(result)


# ─── POST /benefits ────────────────────────────────────────────────────────────

@router.post("")
async def create_benefit(
    body:   dict = Body(...),
    claims: dict = Depends(current_claims),
    conn:   Session = Depends(tenant_conn),
) -> dict:
    """Create a benefit (four-eyes per the bank's Controls switch)."""
    _require_module(claims)
    _institution_id(claims)

    rec = _normalise(conn, body)
    return _submit(conn, "benefit.create", None, json.loads(_payload(rec)))


# ─── PUT /benefits/{id} ────────────────────────────────────────────────────────

@router.put("/{benefit_id}")
async def update_benefit(
    benefit_id: str  = Path(...),
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
    conn:        Session = Depends(tenant_conn),
) -> dict:
    """Update a benefit (four-eyes per the bank's Controls switch)."""
    _require_module(claims)
    institution_id = _institution_id(claims)

    # Verify ownership
    existing = conn.execute(
        text("SELECT * FROM institution.benefit WHERE id = :id"),
        {"id": benefit_id},
    ).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Benefit not found.")
    if str(existing.institution_id) != institution_id:
        raise HTTPException(status_code=403, detail="Not your benefit.")

    current = dict(existing._mapping)
    rec = _normalise(conn, body, current)
    # The full record goes to the checker: benefit.update writes every field,
    # so a partial body would blank the rest.
    payload = json.loads(_payload(rec))
    payload["benefit_id"] = benefit_id
    payload["payload_before"] = json.loads(json.dumps(current, default=str))
    return _submit(conn, "benefit.update", benefit_id, payload)


# ─── DELETE /benefits/{id} (soft-delete via is_active=false) ──────────────────

@router.delete("/{benefit_id}")
async def deactivate_benefit(
    benefit_id: str  = Path(...),
    claims:     dict = Depends(current_claims),
    conn:       Session = Depends(tenant_conn),
) -> dict:
    """Deactivate a benefit (soft delete, keeps bid_benefit snapshots), four-eyes
    per the bank's Controls switch."""
    _require_module(claims)
    institution_id = _institution_id(claims)
    owned = conn.execute(
        text("SELECT 1 FROM institution.benefit WHERE id = :id AND institution_id = :iid"),
        {"id": benefit_id, "iid": institution_id},
    ).fetchone()
    if owned is None:
        raise HTTPException(status_code=404, detail="Benefit not found.")
    return _submit(conn, "benefit.delete", benefit_id, {"benefit_id": benefit_id})
