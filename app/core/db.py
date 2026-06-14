# =============================================================================
# ficium-portal-api — Database core (psycopg2 + SQLAlchemy)
#
# Uses psycopg2 (sync) via SQLAlchemy connection pool.
# asyncpg requires a direct TCP connection to Postgres (port 5432) which
# Supabase blocks from external IPs on the free plan. psycopg2 works fine
# with the Supabase transaction pooler (port 6543, pgbouncer).
#
# The RLS contract is identical: every request sets request.jwt.claims and
# SET LOCAL ROLE authenticated inside a transaction, so auth.uid() resolves
# correctly and RLS enforces tenant isolation unchanged.
# =============================================================================

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Generator

import structlog
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

log = structlog.get_logger()

_engine = None
_SessionLocal = None


def init_pool() -> None:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_min,
            max_overflow=settings.db_pool_max - settings.db_pool_min,
            pool_pre_ping=True,
            connect_args={"sslmode": "require"},
        )
        _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
        log.info("db_pool_ready", min=settings.db_pool_min, max=settings.db_pool_max)


def close_pool() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _SessionLocal = None
        log.info("db_pool_closed")


def _session_or_raise() -> sessionmaker:
    if _SessionLocal is None:
        raise RuntimeError("DB pool not initialised — call init_pool() at startup.")
    return _SessionLocal


@contextmanager
def tenant_session(claims: dict[str, Any]) -> Generator[Session, None, None]:
    """
    Yield a DB session scoped to ONE request's identity.
    Sets request.jwt.claims and ROLE authenticated so RLS works
    identically to PostgREST / the asyncpg version.
    """
    SessionLocal = _session_or_raise()
    claims_json = json.dumps(claims, separators=(",", ":"))
    session = SessionLocal()
    try:
        session.execute(text("SELECT set_config('request.jwt.claims', :c, true)"),
                        {"c": claims_json})
        session.execute(text("SET LOCAL ROLE authenticated"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def service_session() -> Generator[Session, None, None]:
    """Privileged session for admin operations outside tenant RLS scope."""
    SessionLocal = _session_or_raise()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
