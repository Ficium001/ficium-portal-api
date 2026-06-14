# =============================================================================
# ficium-portal-api — Database core
# File: app/core/db.py
#
# THE KEYSTONE OF THE PORTABLE ARCHITECTURE.
#
# This module is the exact replacement for what Supabase PostgREST did for us:
# for every authenticated request it opens a transaction, drops privileges to
# the `authenticated` role, and injects the verified ficium-auth JWT claims
# into `request.jwt.claims`. From that point on EVERY existing RLS policy and
# SECURITY DEFINER function (current_member_ctx, submit_for_approval,
# approve_action, ...) behaves identically to how it did on Supabase — without
# a single line of SQL changing.
#
# Tenant isolation is therefore enforced by Postgres itself (RLS FORCED on
# tenant tables), not by application code remembering to add `WHERE
# institution_id = ...`. The API layer is a thin, auditable transport.
#
# Portability: the connection string is the ONLY thing that changes between
# deployment models —
#   SaaS              → Supabase Postgres (direct connection, not PostgREST)
#   client cloud / MRU → managed Postgres in-region (data residency satisfied)
#   on-premises       → Postgres inside the institution's own environment
# In every case the same migrations + db/000_auth_shim.sql are loaded and this
# module behaves the same.
# =============================================================================

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import structlog

from .config import settings

log = structlog.get_logger()

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the shared connection pool. Called once at app startup."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            command_timeout=settings.db_command_timeout,
            # Statement cache off: tenant-scoped sessions reset role/GUCs per
            # transaction, and pgbouncer (transaction pooling) requires it.
            statement_cache_size=0,
        )
        log.info("db_pool_ready", min=settings.db_pool_min, max=settings.db_pool_max)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db_pool_closed")


def _pool_or_raise() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() at startup.")
    return _pool


@asynccontextmanager
async def tenant_session(claims: dict[str, Any]) -> AsyncIterator[asyncpg.Connection]:
    """
    Yield a connection bound to ONE request's identity, inside a transaction:

      1. SET LOCAL ROLE authenticated   — drop superuser/owner privileges so
         RLS is actually enforced (owners bypass RLS unless FORCE is set; we
         never rely on that — we always run as `authenticated`).
      2. SET LOCAL request.jwt.claims = <verified claims>  — exactly what
         PostgREST injected; auth.uid()/auth.jwt() now resolve correctly.

    Both settings are transaction-scoped (LOCAL), so they cannot leak to the
    next user of a pooled connection. Commit on success, rollback on error.

    `claims` MUST already be signature-verified (see app/core/security.py).
    Passing unverified claims here would defeat the entire model.
    """
    pool = _pool_or_raise()
    claims_json = json.dumps(claims, separators=(",", ":"))

    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            # Order matters: set the claims GUC, then drop role. set_config with
            # is_local=true scopes both to this transaction only.
            await conn.execute("SELECT set_config('request.jwt.claims', $1, true)", claims_json)
            await conn.execute("SET LOCAL ROLE authenticated")
            yield conn
            await tx.commit()
        except Exception:
            await tx.rollback()
            raise


@asynccontextmanager
async def service_session() -> AsyncIterator[asyncpg.Connection]:
    """
    Privileged session for operations that legitimately run outside any single
    tenant's RLS scope — user provisioning (replacing the Supabase Edge
    Function), cross-tenant admin reporting, scheduled expiry of pending
    actions. Use SPARINGLY and never with browser-supplied filters: every such
    call must apply its own institution_id scoping explicitly.
    """
    pool = _pool_or_raise()
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            yield conn
            await tx.commit()
        except Exception:
            await tx.rollback()
            raise
