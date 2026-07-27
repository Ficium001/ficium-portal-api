# =============================================================================
# ficium-portal-api — Database core (psycopg2 + SQLAlchemy)
#
# Uses psycopg2 (sync) via SQLAlchemy connection pool.
# Connects via Supabase transaction pooler (port 6543, pgbouncer).
#
# RLS contract: every request, inside tenant_session():
#   1. set_config('request.jwt.claims', ...) so auth.uid() / auth.jwt()
#      resolve for RLS policies.
#   2. SET LOCAL ROLE authenticated, so RLS is actually evaluated. The
#      pooler connects as `postgres`, which carries BYPASSRLS — without
#      this role switch every RLS policy on every table is silently
#      inert, regardless of how correct the policy itself is. (This was
#      previously omitted — see incident notes below.)
#
# Both are SET LOCAL / transaction-scoped, auto-resetting on COMMIT or
# ROLLBACK, which is required and safe under pgbouncer transaction-mode
# pooling (session state resets between transactions anyway).
#
# `authenticated` must hold USAGE + table grants on every schema/table the
# routers touch (institution, marketplace, catalog, governance, audit,
# portal_admin, admin) — verified current as of 2026-06-30.
#
# INCIDENT (2026-06-30): the SET LOCAL ROLE step was missing entirely.
# Every tenant_conn-based endpoint ran as `postgres` (BYPASSRLS=true),
# so all RLS policies — not just product/institution scoping added that
# day, every pre-existing one too — were never enforced; only each
# endpoint's own explicit WHERE clauses provided any isolation, and at
# least one endpoint (GET /marketplace/requests) had none. Restored the
# role switch and granted the few tables that were missing `authenticated`
# grants as a result (institution.doc, institution.doc_type,
# institution.compliance, marketplace.bid_benefit).
# =============================================================================

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

log = structlog.get_logger()

_engine = None
_SessionLocal = None

# Cross-project engine for the Ficium App database (marketplace pool).
# Lazily created only if app_database_url is configured.
_app_engine = None
_AppSessionLocal = None


def init_pool() -> None:
    global _engine, _SessionLocal, _app_engine, _AppSessionLocal
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_min,
            max_overflow=settings.db_pool_max - settings.db_pool_min,
            pool_pre_ping=True,
            # Recycle connections before Supabase's pooler drops idle ones
            # (default server idle timeout is ~10 min). Without this, a
            # connection can go stale in the pool and the next request eats a
            # "server closed the connection unexpectedly" before pre_ping
            # replaces it. 5 min stays safely under the server timeout.
            pool_recycle=300,
            connect_args={
                "sslmode": "require",
                # TCP keepalives keep long-lived pooled connections from being
                # silently reaped by intermediate NAT/load balancers.
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        )
        _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
        log.info("db_pool_ready", min=settings.db_pool_min, max=settings.db_pool_max)

    if _app_engine is None and settings.app_database_url:
        _app_engine = create_engine(
            settings.app_database_url,
            pool_size=settings.db_pool_min,
            max_overflow=settings.db_pool_max - settings.db_pool_min,
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args={
                "sslmode": "require",
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        )
        _AppSessionLocal = sessionmaker(bind=_app_engine, autocommit=False, autoflush=False)
        log.info("app_db_pool_ready")
    elif not settings.app_database_url:
        log.warning("app_db_pool_skipped", reason="APP_DATABASE_URL not set")


def close_pool() -> None:
    global _engine, _SessionLocal, _app_engine, _AppSessionLocal
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _SessionLocal = None
        log.info("db_pool_closed")
    if _app_engine is not None:
        _app_engine.dispose()
        _app_engine = None
        _AppSessionLocal = None
        log.info("app_db_pool_closed")


def _session_or_raise() -> sessionmaker:
    if _SessionLocal is None:
        raise RuntimeError("DB pool not initialised — call init_pool() at startup.")
    return _SessionLocal


@contextmanager
def tenant_session(claims: dict[str, Any]) -> Generator[Session]:
    """
    Yield a DB session scoped to ONE request's identity, with RLS ENFORCED.

    Two steps inside the transaction, in this order:
      1. set_config('request.jwt.claims', ...) so auth.uid() / auth.jwt()
         resolve for the RLS policies.
      2. SET LOCAL ROLE authenticated so Postgres actually APPLIES those
         policies. The pooler logs in as the `postgres` role, which carries
         BYPASSRLS — without this role switch, RLS never fires and tenant
         isolation depends entirely on each query remembering its WHERE
         clause. Switching to `authenticated` (no BYPASSRLS) makes RLS the
         real backstop: a forgotten filter can no longer leak across tenants.

    Both are SET LOCAL / transaction-scoped, so they auto-reset on COMMIT or
    ROLLBACK — safe under pgbouncer transaction-mode pooling, which resets
    session state between transactions anyway.

    NOTE: `authenticated` must hold USAGE + the necessary table grants on
    every schema the routers touch (admin, catalog, identity, institution,
    marketplace, governance, portal_admin). Migrations 09 and 10 establish
    these grants and the catalog read policies. Privileged work that must
    bypass RLS uses service_session() instead.
    """
    SessionLocal = _session_or_raise()
    claims_json = json.dumps(claims, separators=(",", ":"))
    session = SessionLocal()
    try:
        # set_config publishes JWT claims so auth.uid() / auth.jwt() resolve
        # for RLS policies.
        session.execute(
            text("SELECT set_config('request.jwt.claims', :c, true)"),
            {"c": claims_json},
        )
        # SET LOCAL ROLE authenticated — drops BYPASSRLS so the policies set
        # above actually get evaluated. The pooler connects as `postgres`
        # (rolbypassrls=true, confirmed via pg_roles); without this, every
        # RLS policy on every table is a no-op regardless of how it's
        # written. `postgres` is already a member of `authenticated` (no
        # extra GRANT needed for the role switch itself — confirmed via
        # pg_auth_members), and `authenticated` holds USAGE + the required
        # table grants on every schema/table the routers touch.
        session.execute(text("SET LOCAL ROLE authenticated"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def service_session() -> Generator[Session]:
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


class AppDatabaseUnavailable(RuntimeError):
    """Raised when APP_DATABASE_URL is not configured but an App read is attempted."""


@contextmanager
def app_service_session() -> Generator[Session]:
    """
    Read-only session against the Ficium App database (cross-project).
    Used by the marketplace, which reads public.requests and
    public.client_financial_snapshot from the App Supabase project.

    Raises AppDatabaseUnavailable (mapped to HTTP 503 by the router) if
    APP_DATABASE_URL is not configured, so a missing var surfaces as a
    clear "temporarily unavailable" rather than an opaque 500.
    """
    if _AppSessionLocal is None:
        raise AppDatabaseUnavailable(
            "APP_DATABASE_URL not configured — marketplace reads are unavailable."
        )
    session = _AppSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
