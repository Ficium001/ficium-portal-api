"""Shared wording and helpers for approval-engine errors (db/065)."""

from __future__ import annotations

# Routing refuses an approval that nobody other than the submitter could approve.
NO_APPROVER_MESSAGE = (
    "Nobody else can approve this yet. Give a colleague the required role "
    "(or committee seat) under Setup > Team, then submit again."
)


def pg_detail(exc: BaseException) -> str:
    """The DETAIL of a Postgres error raised through SQLAlchemy/psycopg2, or ''."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return (getattr(diag, "message_detail", None) or "").strip()
