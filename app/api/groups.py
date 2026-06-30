# =============================================================================
# ficium-portal-api — Groups router
# Replaces direct Supabase calls in GroupsTab.tsx and TeamTab invite.
#
# GET  /groups                  → institution.group (RLS-scoped)
# GET  /groups/pending          → institution.pending_actions for group.* actions
# GET  /groups/my-modules       → get_my_modules() RPC (module picker)
# GET  /groups/my-products      → get_my_products() RPC (product picker — own scope)
# GET  /groups/licensed-products → institution.product_config × catalog.product
#                                   (full set of products the institution may grant)
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import tenant_conn

router = APIRouter(prefix="/groups", tags=["groups"])


def _row(row) -> dict:
    m = dict(row._mapping)
    # module_permissions comes back as a Python list from psycopg2 — keep it
    if not isinstance(m.get("module_permissions"), list):
        m["module_permissions"] = []
    # product_scope: NULL/empty => unrestricted. Keep as [] for the frontend
    # rather than null, so it can be checked with .length === 0 like modules.
    if not isinstance(m.get("product_scope"), list):
        m["product_scope"] = []
    else:
        m["product_scope"] = [str(p) for p in m["product_scope"]]
    # Serialise timestamps
    for k in ("created_at", "updated_at"):
        if m.get(k) is not None:
            m[k] = m[k].isoformat()
    # UUIDs → str
    for k in ("id", "institution_id", "created_by"):
        if m.get(k) is not None:
            m[k] = str(m[k])
    return m


@router.get("")
async def list_groups(
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """All institution groups for the caller's institution (RLS-scoped)."""
    rows = conn.execute(
        text("""
            SELECT id, institution_id, slug, label, description,
                   module_permissions, product_scope, is_system, created_by,
                   created_at, updated_at
            FROM institution."group"
            ORDER BY created_at ASC
        """)
    ).fetchall()
    return [_row(r) for r in rows]


@router.get("/pending")
async def list_pending_group_actions(
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """
    Pending maker-checker actions with category group.* for this institution.
    Replaces the defunct pending_actions_v Supabase view.
    """
    rows = conn.execute(
        text("""
            SELECT
                id,
                action_category,
                action_status,
                maker_id,
                maker_role,
                institution_id,
                initiated_at,
                resource_type,
                resource_id,
                payload,
                payload_before,
                checker_id,
                checker_role,
                checker_note,
                checked_at,
                expires_at,
                executed_at,
                execution_error,
                created_at
            FROM institution.pending_actions
            WHERE action_status = 'pending'
              AND action_category LIKE 'group.%%'
            ORDER BY initiated_at DESC
        """)
    ).fetchall()

    result = []
    for r in rows:
        m = dict(r._mapping)
        for k in ("id", "maker_id", "institution_id", "checker_id", "resource_id"):
            if m.get(k) is not None:
                m[k] = str(m[k])
        for k in ("initiated_at", "checked_at", "expires_at", "executed_at", "created_at"):
            if m.get(k) is not None:
                m[k] = m[k].isoformat()
        result.append(m)
    return result


@router.get("/my-modules")
async def get_my_modules(
    conn: Session = Depends(tenant_conn),
) -> list[str]:
    """
    Module keys the calling member may grant (via get_my_modules RPC).
    Returns [] if the member has no custom or system group.
    Falls back to [] (not an error) so the UI shows the full catalogue.
    """
    try:
        row = conn.execute(text("SELECT get_my_modules() AS modules")).fetchone()
        if row is None or row.modules is None:
            return []
        return list(row.modules)
    except Exception:
        return []


@router.get("/my-products")
async def get_my_products(
    conn: Session = Depends(tenant_conn),
) -> list[str]:
    """
    Product ids the calling member may grant (via get_my_products RPC).
    Returns [] for an unrestricted caller (NULL/empty product_scope on
    their own group) — the UI then offers the full licensed-products list.
    Mirrors get_my_modules exactly.
    """
    try:
        row = conn.execute(text("SELECT get_my_products() AS products")).fetchone()
        if row is None or row.products is None:
            return []
        return [str(p) for p in row.products]
    except Exception:
        return []


@router.get("/licensed-products")
async def list_licensed_products(
    conn: Session = Depends(tenant_conn),
) -> list[dict]:
    """
    Full set of products this institution is licensed for
    (institution.product_config.enabled = true, RLS-scoped), joined to
    catalog.product for display labels. This is the "full catalogue" the
    product picker falls back to when the caller is unrestricted —
    the per-institution analogue of the static INSTITUTION_MODULE_LIST.
    """
    rows = conn.execute(
        text("""
            SELECT p.id, p.code, p.label, pf.code AS family_code, pf.label AS family_label
            FROM institution.product_config pc
            JOIN catalog.product p ON p.id = pc.product_id
            JOIN catalog.product_family pf ON pf.id = p.family_id
            WHERE pc.enabled = true AND p.active = true
            ORDER BY pf.sort_order, p.sort_order
        """)
    ).fetchall()
    return [
        {
            "id": str(r.id),
            "code": r.code,
            "label": r.label,
            "family_code": r.family_code,
            "family_label": r.family_label,
        }
        for r in rows
    ]
