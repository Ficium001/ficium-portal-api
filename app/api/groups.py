# =============================================================================
# ficium-portal-api — Groups router
# Replaces direct Supabase calls in GroupsTab.tsx and TeamTab invite.
#
# GET  /groups              → institution.group (RLS-scoped)
# GET  /groups/pending      → institution.pending_actions for group.* actions
# GET  /groups/my-modules   → get_my_modules() RPC (module picker)
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import current_claims, tenant_conn

router = APIRouter(prefix="/groups", tags=["groups"])


def _row(row) -> dict:
    m = dict(row._mapping)
    # module_permissions comes back as a Python list from psycopg2 — keep it
    if not isinstance(m.get("module_permissions"), list):
        m["module_permissions"] = []
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
                   module_permissions, is_system, created_by,
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
