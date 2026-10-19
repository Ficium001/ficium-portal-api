# =============================================================================
# ficium-portal-api — Controls (four-eyes switches), Setup > Controls
#
#   GET  /controls/four-eyes                     every action, its screen, on/off,
#                                                 locked?, pending switch-off
#   POST /controls/four-eyes/{category}/enable   immediate (admins)
#   POST /controls/four-eyes/{category}/disable  queues four_eyes.disable for a
#                                                 second admin (locked four-eyes)
#
# Enforcement lives in the database (db/040): governance.submit consults the
# switch on every action, locked categories can never be switched off, and
# only institution admins can change a switch. This router is a thin surface.
# =============================================================================
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/controls", tags=["controls"])

_CATEGORY = re.compile(r"^[a-z_]+\.[a-z_]+$")


def _require_admin(claims: dict[str, Any]) -> None:
    if claims.get("user_role") not in INSTITUTION_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only institution admins can change controls.")


def _category(category: str) -> str:
    if not _CATEGORY.match(category):
        raise HTTPException(status_code=404, detail="Unknown control.")
    return category


def _db_error(e: DBAPIError) -> HTTPException:
    msg = str(getattr(e.orig, "pgerror", None) or e.orig).split("\n")[0]
    msg = msg.removeprefix("ERROR:").strip()
    return HTTPException(status_code=409, detail=msg or "The change was refused.")


@router.get("/four-eyes")
async def list_four_eyes(conn: Session = Depends(tenant_conn)) -> list[dict[str, Any]]:
    rows = conn.execute(text("""
        SELECT c.category, c.label, c.screen, c.locked, c.locked_reason, c.sort_order,
               governance.four_eyes_required(ctx.institution_id, c.category) AS enabled,
               p.changed_at,
               (SELECT a.id FROM governance.action a
                 WHERE a.institution_id = ctx.institution_id
                   AND a.category = 'four_eyes.disable' AND a.status = 'pending'
                   AND a.payload ->> 'category' = c.category
                 ORDER BY a.created_at DESC LIMIT 1) AS pending_disable_action_id
          FROM governance.four_eyes_catalog c
          CROSS JOIN institution.current_member_ctx() ctx
          LEFT JOIN governance.four_eyes_policy p
                 ON p.institution_id = ctx.institution_id AND p.category = c.category
         WHERE c.category <> 'four_eyes.disable'
         ORDER BY c.sort_order, c.category
    """)).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["changed_at"] = d["changed_at"].isoformat() if d["changed_at"] else None
        d["pending_disable_action_id"] = (
            str(d["pending_disable_action_id"]) if d["pending_disable_action_id"] else None
        )
        out.append(d)
    return out


@router.post("/four-eyes/{category}/enable")
async def enable_four_eyes(
    category: str,
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict[str, Any]:
    _require_admin(claims)
    try:
        conn.execute(text("SELECT governance.enable_four_eyes(:c)"), {"c": _category(category)})
    except DBAPIError as e:
        conn.rollback()
        raise _db_error(e) from None
    conn.commit()
    return {"category": category, "enabled": True}


@router.post("/four-eyes/{category}/disable")
async def request_disable_four_eyes(
    category: str,
    claims: dict = Depends(current_claims),
    conn: Session = Depends(tenant_conn),
) -> dict[str, Any]:
    _require_admin(claims)
    try:
        row = conn.execute(
            text("""
                SELECT institution.submit_for_approval(
                  'four_eyes.disable', 'four_eyes_policy', NULL, CAST(:p AS jsonb)) AS id
            """),
            {"p": json.dumps({"category": _category(category)})},
        ).fetchone()
    except DBAPIError as e:
        conn.rollback()
        raise _db_error(e) from None
    conn.commit()
    return {
        "category": category,
        "status": "pending_approval",
        "action_id": str(row.id) if row else None,
    }
