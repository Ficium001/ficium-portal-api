# =============================================================================
# ficium-portal-api — Notifications router
# =============================================================================
# Provides read/write access to portal_notifications for institution members.
#
# The table is written by:
#   - This API (on pipeline advance, approval events, bid acceptance)
#   - The public.py server-to-server endpoint (on bid accepted from app DB)
#
# The portal reads through this router, not the Supabase client: portal users
# sign in with ficium-auth, so a direct Supabase read has no session and 401s
# (it did, every 30 s, until Oct 2026).
#
# Endpoints:
#   GET  /notifications                  latest notifications for the institution
#   GET  /notifications/unread-count     badge count
#   POST /notifications/{id}/mark-read   mark one read
#   POST /notifications/mark-all-read    mark all read
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from ..core.db import service_session
from ..deps import current_claims

router = APIRouter(prefix="/notifications", tags=["notifications"])

# ---------------------------------------------------------------------------
# Internal helper — used by pipeline.py, approvals.py, marketplace.py
# ---------------------------------------------------------------------------
def _write_notification(
    conn,
    institution_id: str,
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    metadata: dict | None = None,
) -> None:
    """
    Insert a portal notification record. Idempotent on title+institution_id
    within the last 60 seconds to prevent duplicate events from rapid mutations.
    """
    conn.execute(text("""
        INSERT INTO public.portal_notifications
            (institution_id, kind, title, body, link, metadata)
        SELECT :iid, :kind, :title, :body, :link, CAST(:meta AS jsonb)
        WHERE NOT EXISTS (
            SELECT 1 FROM public.portal_notifications
            WHERE institution_id = :iid
              AND title          = :title
              AND created_at     > now() - interval '60 seconds'
        )
    """), {
        "iid":   institution_id,
        "kind":  kind,
        "title": title,
        "body":  body,
        "link":  link,
        "meta":  __import__("json").dumps(metadata) if metadata else None,
    })


# ---------------------------------------------------------------------------
# GET /notifications — latest for the caller's institution
# ---------------------------------------------------------------------------
@router.get("")
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    claims: dict = Depends(current_claims),
) -> list[dict]:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as conn:
        rows = conn.execute(text("""
            SELECT id, kind, title, body, link, read_at, created_at, metadata
            FROM public.portal_notifications
            WHERE institution_id = :iid
            ORDER BY created_at DESC
            LIMIT :lim
        """), {"iid": institution_id, "lim": limit}).mappings().all()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /notifications/unread-count — summary count for shell badge
# ---------------------------------------------------------------------------
@router.get("/unread-count")
async def unread_count(claims: dict = Depends(current_claims)) -> dict:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as conn:
        row = conn.execute(text("""
            SELECT count(*) AS n
            FROM public.portal_notifications
            WHERE institution_id = :iid AND read_at IS NULL
        """), {"iid": institution_id}).fetchone()
        return {"unread": row.n if row else 0}


# ---------------------------------------------------------------------------
# POST /notifications/mark-read — mark one notification read
# ---------------------------------------------------------------------------
@router.post("/{notification_id}/mark-read")
async def mark_read(
    notification_id: str,
    claims: dict = Depends(current_claims),
) -> dict:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as conn:
        conn.execute(text("""
            UPDATE public.portal_notifications
            SET read_at = now()
            WHERE id = :id AND institution_id = :iid AND read_at IS NULL
        """), {"id": notification_id, "iid": institution_id})
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /notifications/mark-all-read
# ---------------------------------------------------------------------------
@router.post("/mark-all-read")
async def mark_all_read(claims: dict = Depends(current_claims)) -> dict:
    institution_id = claims.get("institution_id")
    if not institution_id:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")

    with service_session() as conn:
        rows = conn.execute(text("""
            UPDATE public.portal_notifications
            SET read_at = now()
            WHERE institution_id = :iid AND read_at IS NULL
            RETURNING id
        """), {"iid": institution_id}).fetchall()
        conn.commit()
        return {"ok": True, "marked": len(rows)}
