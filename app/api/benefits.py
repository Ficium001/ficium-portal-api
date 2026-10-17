# =============================================================================
# ficium-portal-api — Benefits router
#
# Module gate:  inst:benefits
# Write model:  guaranteed benefits → institution.pending_actions (maker-checker)
#               non-guaranteed      → direct (low commercial risk)
# Read model:   direct, institution-scoped via RLS
#
# Endpoints:
#   GET  /benefits/categories          — Ficium-seeded categories (reference)
#   GET  /benefits                     — institution's active benefits
#   POST /benefits                     — create (guaranteed → pending, else direct)
#   PUT  /benefits/{id}                — update (guaranteed → pending, else direct)
#   DELETE /benefits/{id}              — deactivate (always direct — low risk)
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
    """
    Create a benefit.
    Guaranteed benefits (is_guaranteed=true) route through maker-checker.
    Non-guaranteed are created directly.
    """
    _require_module(claims)
    institution_id = _institution_id(claims)
    member_id      = claims.get("member_id") or claims.get("sub")

    rec = _normalise(conn, body)
    is_guaranteed = bool(body.get("is_guaranteed", False))

    if is_guaranteed:
        # Route through maker-checker via submit_for_approval RPC.
        # The DB function resolves maker_id from auth.uid() (JWT claims GUC).
        result = conn.execute(
            text("""
                SELECT institution.submit_for_approval(
                    :cat, :rtype, NULL, CAST(:payload AS jsonb)
                ) AS action_id
            """),
            {"cat": "benefit.create", "rtype": "benefit", "payload": _payload(rec)},
        ).fetchone()
        conn.commit()
        if result is None:
            raise HTTPException(status_code=500, detail="submit_for_approval returned no action id.")
        return {"pending": True, "action_id": str(result.action_id)}

    # Non-guaranteed — direct insert
    row = conn.execute(
        text("""
            INSERT INTO institution.benefit (
                institution_id, product_id, cat_id,
                title, description, value_display, value_amount, value_unit,
                is_guaranteed, conditions, valid_from, valid_until,
                is_active, created_by
            ) VALUES (
                :iid, :product_id, :cat_id,
                :title, :description, :value_display, :value_amount, :value_unit,
                false, :conditions, :valid_from, :valid_until,
                true, :created_by
            )
            RETURNING *
        """),
        {
            "iid":           institution_id,
            "product_id":    rec["product_id"],
            "cat_id":        rec["cat_id"],
            "title":         rec["title"],
            "description":   rec["description"],
            "value_display": rec["value_display"],
            "value_amount":  rec["value_amount"],
            "value_unit":    rec["value_unit"],
            "conditions":    rec["conditions"],
            "valid_from":    rec["valid_from"],
            "valid_until":   rec["valid_until"],
            "created_by":    member_id,
        },
    ).fetchone()
    conn.commit()
    if row is None:
        raise HTTPException(status_code=500, detail="Benefit record could not be created.")
    return dict(row._mapping)


# ─── PUT /benefits/{id} ────────────────────────────────────────────────────────

@router.put("/{benefit_id}")
async def update_benefit(
    benefit_id: str  = Path(...),
    body:        dict = Body(...),
    claims:      dict = Depends(current_claims),
    conn:        Session = Depends(tenant_conn),
) -> dict:
    """
    Update a benefit.
    Changing is_guaranteed=true always routes through maker-checker.
    """
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
    is_guaranteed = bool(body.get("is_guaranteed", existing.is_guaranteed))

    if is_guaranteed:
        # The full record goes to the checker: benefit.update writes every
        # field, so a partial body would have blanked the rest. payload_before
        # is serialised with default=str (uuid/date values made json.dumps
        # raise before: every guaranteed edit returned 500).
        payload = json.loads(_payload(rec))
        payload["benefit_id"] = benefit_id
        payload["payload_before"] = json.loads(json.dumps(current, default=str))
        result = conn.execute(
            text("""
                SELECT institution.submit_for_approval(
                    :cat, :rtype, :rid, CAST(:payload AS jsonb)
                ) AS action_id
            """),
            {"cat": "benefit.update", "rtype": "benefit", "rid": benefit_id,
             "payload": json.dumps(payload)},
        ).fetchone()
        conn.commit()
        if result is None:
            raise HTTPException(status_code=500, detail="submit_for_approval returned no action id.")
        return {"pending": True, "action_id": str(result.action_id)}

    row = conn.execute(
        text("""
            UPDATE institution.benefit
               SET product_id = :product_id, cat_id = :cat_id, title = :title,
                   description = :description, value_display = :value_display,
                   value_amount = :value_amount, value_unit = :value_unit,
                   conditions = :conditions, valid_from = :valid_from,
                   valid_until = :valid_until,
                   is_active = COALESCE(:is_active, is_active), updated_at = now()
             WHERE id = :id
            RETURNING *
        """),
        {k: rec.get(k) for k in ("product_id", "cat_id", "title", "description",
                                  "value_display", "value_amount", "value_unit",
                                  "conditions", "valid_from", "valid_until", "is_active")}
        | {"id": benefit_id},
    ).fetchone()
    conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="Benefit not found or no changes applied.")
    return dict(row._mapping)


# ─── DELETE /benefits/{id} (soft-delete via is_active=false) ──────────────────

@router.delete("/{benefit_id}")
async def deactivate_benefit(
    benefit_id: str  = Path(...),
    claims:     dict = Depends(current_claims),
    conn:       Session = Depends(tenant_conn),
) -> dict:
    """Deactivate a benefit (soft delete — preserves bid_benefit snapshots)."""
    _require_module(claims)
    institution_id = _institution_id(claims)

    row = conn.execute(
        text("""
            UPDATE institution.benefit
            SET is_active = false, updated_at = now()
            WHERE id = :id AND institution_id = :iid
            RETURNING id, is_active
        """),
        {"id": benefit_id, "iid": institution_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Benefit not found.")
    conn.commit()
    return {"ok": True, "id": benefit_id}
