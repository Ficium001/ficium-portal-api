# =============================================================================
# ficium-portal-api — Notifications router
# =============================================================================
# Provides read/write access to portal_notifications for institution members.
#
# The table is written by:
#   - This API (on pipeline advance, approval events, bid acceptance)
#   - The public.py server-to-server endpoint (on bid accepted from app DB)
#
# Reads are handled directly by the portal UI via the Supabase client with
# RLS (institution_members_read_own_notifications policy). This router handles
# the server-side writes that require service_role access.
#
# Endpoints:
#   POST /notifications/write        Write a notification (service-to-service)
#   GET  /notifications              List for current institution (via portal client)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
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
# GET /notifications — summary count for shell badge
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
