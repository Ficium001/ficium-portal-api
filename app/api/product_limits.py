# =============================================================================
# ficium-portal-api — Product limits (Products page)
#
#   GET /institution/product-limits               limits for every licensed product
#   PUT /institution/product-limits/{product_id}  set rate / amount / term limits (admin)
#
# Lives on institution.product_config, the same row that licenses the
# institution to bid on the product (core/bidding.py's gate). Ficium sets
# licensing (enabled); the institution sets its own limits within the
# platform's caps (catalog.product.min_amount / max_amount).
#
# Until Oct 2026 the Products page wrote straight to the Portal DB through
# the Supabase client, to a table name that no longer existed
# (institution_product_config, renamed product_config), with no session
# (portal users sign in with ficium-auth), ignored the error and showed
# "Saved". No limit was ever stored.
#
# Rates are fractions (0.085 = 8.5 % a year), as the page already sent them.
# =============================================================================
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from ..core.db import service_session
from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..deps import current_claims

router = APIRouter(prefix="/institution/product-limits", tags=["products"])


class ProductLimitsInput(BaseModel):
    min_rate:        Decimal | None = Field(default=None, ge=0, le=1)
    max_rate:        Decimal | None = Field(default=None, ge=0, le=1)
    min_amount:      Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    max_amount:      Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    min_term_months: int | None = Field(default=None, ge=1, le=600)
    max_term_months: int | None = Field(default=None, ge=1, le=600)

    @model_validator(mode="after")
    def _ordered(self) -> ProductLimitsInput:
        for lo, hi, label in (("min_rate", "max_rate", "rate"),
                              ("min_amount", "max_amount", "amount"),
                              ("min_term_months", "max_term_months", "term")):
            a, b = getattr(self, lo), getattr(self, hi)
            if a is not None and b is not None and a > b:
                raise ValueError(f"The minimum {label} can't be above the maximum.")
        return self


_COLS = ("product_id, enabled, min_rate, max_rate, min_amount, max_amount, "
         "min_term_months, max_term_months, updated_at")


def _iid(claims: dict[str, Any]) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    return str(iid)


@router.get("")
async def list_limits(claims: dict = Depends(current_claims)) -> list[dict]:
    iid = _iid(claims)
    with service_session() as conn:
        rows = conn.execute(text(f"""
            SELECT {_COLS} FROM institution.product_config
             WHERE institution_id = CAST(:iid AS uuid)
        """), {"iid": iid}).mappings().all()
    return [dict(r) for r in rows]


@router.put("/{product_id}")
async def set_limits(
    product_id: str,
    body: ProductLimitsInput = Body(...),
    claims: dict = Depends(current_claims),
) -> dict:
    iid = _iid(claims)
    if claims.get("user_role", "") not in INSTITUTION_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Requires institution admin.")

    with service_session() as conn:
        cap = conn.execute(text("""
            SELECT pc.id, pc.enabled, p.min_amount AS cap_min, p.max_amount AS cap_max
              FROM institution.product_config pc
              JOIN catalog.product p ON p.id = pc.product_id
             WHERE pc.institution_id = CAST(:iid AS uuid) AND pc.product_id = CAST(:pid AS uuid)
             FOR UPDATE OF pc
        """), {"iid": iid, "pid": product_id}).mappings().first()
        if cap is None or not cap["enabled"]:
            raise HTTPException(status_code=409,
                detail="Your institution isn't licensed for this product. Ask Ficium to enable it.")
        for v in (body.min_amount, body.max_amount):
            if v is None:
                continue
            if cap["cap_min"] is not None and v < cap["cap_min"]:
                raise HTTPException(status_code=422,
                    detail=f"Amounts can't be below the platform minimum of {cap['cap_min']:,.0f}.")
            if cap["cap_max"] is not None and v > cap["cap_max"]:
                raise HTTPException(status_code=422,
                    detail=f"Amounts can't be above the platform maximum of {cap['cap_max']:,.0f}.")

        row = conn.execute(text(f"""
            UPDATE institution.product_config
               SET min_rate = :min_rate, max_rate = :max_rate,
                   min_amount = :min_amount, max_amount = :max_amount,
                   min_term_months = :min_term, max_term_months = :max_term,
                   updated_at = now()
             WHERE id = :id
            RETURNING {_COLS}
        """), {
            "id": cap["id"], "min_rate": body.min_rate, "max_rate": body.max_rate,
            "min_amount": body.min_amount, "max_amount": body.max_amount,
            "min_term": body.min_term_months, "max_term": body.max_term_months,
        }).mappings().one()
        conn.execute(text("""
            INSERT INTO audit.event
                (id, occurred_at, actor_id, actor_type, actor_email, actor_role, institution_id,
                 action, resource_type, resource_id, outcome, metadata)
            VALUES (gen_random_uuid(), now(), CAST(:actor AS uuid), 'member', :email, :role,
                    CAST(:iid AS uuid), 'product.limits_updated', 'product_config',
                    CAST(:rid AS uuid), 'success', CAST(:meta AS jsonb))
        """), {"actor": claims.get("sub"), "email": claims.get("email"),
               "role": claims.get("user_role"), "iid": iid, "rid": str(cap["id"]),
               "meta": json.dumps({"product_id": product_id, **body.model_dump()}, default=str)})
        conn.commit()
    return dict(row)
