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

import uuid

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

    required = {"cat_id", "title"}
    missing  = required - set(body)
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {sorted(missing)}")

    is_guaranteed = bool(body.get("is_guaranteed", False))

    if is_guaranteed:
        # Route through maker-checker via submit_for_approval RPC.
        # The DB function resolves maker_id from auth.uid() (JWT claims GUC).
        import json as _json
        result = conn.execute(
            text("""
                SELECT institution.submit_for_approval(
                    :cat, :rtype, NULL, CAST(:payload AS jsonb)
                ) AS action_id
            """),
            {
                "cat":     "benefit.create",
                "rtype":   "benefit",
                "payload": _json.dumps({**body, "institution_id": institution_id}),
            },
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
                title, description, value_display,
                is_guaranteed, conditions, valid_from, valid_until,
                is_active, created_by
            ) VALUES (
                :iid, :product_id, :cat_id,
                :title, :description, :value_display,
                false, :conditions, :valid_from, :valid_until,
                true, :created_by
            )
            RETURNING *
        """),
        {
            "iid":           institution_id,
            "product_id":    body.get("product_id"),
            "cat_id":        body["cat_id"],
            "title":         body["title"],
            "description":   body.get("description"),
            "value_display": body.get("value_display"),
            "conditions":    body.get("conditions"),
            "valid_from":    body.get("valid_from"),
            "valid_until":   body.get("valid_until"),
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
    member_id      = claims.get("member_id") or claims.get("sub")

    # Verify ownership
    existing = conn.execute(
        text("SELECT * FROM institution.benefit WHERE id = :id"),
        {"id": benefit_id},
    ).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Benefit not found.")
    if str(existing.institution_id) != institution_id:
        raise HTTPException(status_code=403, detail="Not your benefit.")

    is_guaranteed = bool(body.get("is_guaranteed", existing.is_guaranteed))

    if is_guaranteed:
        import json as _json
        result = conn.execute(
            text("""
                SELECT institution.submit_for_approval(
                    :cat, :rtype, :rid, CAST(:payload AS jsonb)
                ) AS action_id
            """),
            {
                "cat":     "benefit.update",
                "rtype":   "benefit",
                "rid":     benefit_id,
                "payload": _json.dumps({**body, "payload_before": dict(existing._mapping) if existing else {}}),
            },
        ).fetchone()
        conn.commit()
        if result is None:
            raise HTTPException(status_code=500, detail="submit_for_approval returned no action id.")
        return {"pending": True, "action_id": str(result.action_id)}

    # Non-guaranteed — direct update
    allowed = {"title", "description", "value_display", "conditions",
               "valid_from", "valid_until", "is_active", "product_id"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=422, detail="No updatable fields provided.")

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    row = conn.execute(
        text(f"UPDATE institution.benefit SET {set_clause}, updated_at = now() WHERE id = :id RETURNING *"),
        {**updates, "id": benefit_id},
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
