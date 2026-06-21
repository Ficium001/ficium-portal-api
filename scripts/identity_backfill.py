#!/usr/bin/env python3
"""
scripts/identity_backfill.py
=============================
Repoints every tenant table's auth_user_id FK from Supabase auth.users.id
to ficium-auth auth_portal.auth_users.id, matched by email.

Usage
-----
  python scripts/identity_backfill.py --dry-run   # safe — no writes
  python scripts/identity_backfill.py --apply     # writes only if dry-run exit 0

Safety model
------------
- Schema-driven discovery via information_schema (no hardcoded table list).
- --apply refuses if any active user is unmatched.
- Every write is logged to public._identity_migration_log before execution.
- Idempotent: rows already pointing at the ficium-auth UUID are skipped.
- Old FK (→ auth.users) is dropped BEFORE the UPDATEs; otherwise the new UUIDs
  (which don't exist in auth.users) would violate the constraint.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]
DRY_RUN_MARKER = "DRY-RUN"


def connect() -> "psycopg2.connection":
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def ensure_log_table(cur: Any) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public._identity_migration_log (
            id          bigserial PRIMARY KEY,
            run_at      timestamptz DEFAULT now(),
            schema_name text NOT NULL,
            table_name  text NOT NULL,
            column_name text NOT NULL,
            old_uuid    uuid NOT NULL,
            new_uuid    uuid NOT NULL,
            email       text NOT NULL,
            dry_run     boolean NOT NULL
        )
    """)


def discover_tenant_columns(cur: Any) -> list[dict]:
    """
    Find every column named auth_user_id across all schemas except pg internals.
    Returns list of {schema, table, column}.
    """
    cur.execute("""
        SELECT table_schema, table_name, column_name
        FROM   information_schema.columns
        WHERE  column_name = 'auth_user_id'
          AND  table_schema NOT IN ('pg_catalog', 'information_schema',
                                    'pg_toast', 'auth')
        ORDER BY table_schema, table_name
    """)
    return [
        {"schema": r["table_schema"], "table": r["table_name"], "column": r["column_name"]}
        for r in cur.fetchall()
    ]


def build_email_map(cur: Any) -> dict[str, str]:
    """
    Map email → ficium-auth UUID from auth_portal.auth_users.
    Normalised to lowercase for case-insensitive matching.
    """
    cur.execute("""
        SELECT id::text, lower(email) FROM auth_portal.auth_users
    """)
    return {row["lower"]: row["id"] for row in cur.fetchall()}


def build_supabase_email_map(cur: Any) -> dict[str, str]:
    """
    Map old Supabase UUID → email from auth.users (if schema exists).
    Falls back to empty dict if the auth schema has no users table.
    """
    try:
        cur.execute("""
            SELECT id::text, lower(email) FROM auth.users
        """)
        return {row["id"]: row["lower"] for row in cur.fetchall()}
    except psycopg2.errors.UndefinedTable:
        cur.connection.rollback()
        return {}


def run(apply: bool) -> int:
    dry_run = not apply
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    ensure_log_table(cur)

    log("Discovering tenant columns…")
    columns = discover_tenant_columns(cur)
    if not columns:
        log("No auth_user_id columns found — nothing to do.")
        conn.rollback()
        return 0

    for c in columns:
        log(f"  Found: {c['schema']}.{c['table']}.{c['column']}")

    log("Building email maps…")
    ficium_map = build_email_map(cur)          # email → ficium UUID
    supabase_map = build_supabase_email_map(cur)  # old UUID → email
    log(f"  ficium-auth users: {len(ficium_map)}")
    log(f"  supabase auth.users: {len(supabase_map)}")

    total_rows = 0
    unmatched: list[dict] = []
    plan: list[dict] = []

    for col in columns:
        schema, table, column = col["schema"], col["table"], col["column"]
        cur.execute(
            f'SELECT DISTINCT "{column}"::text FROM "{schema}"."{table}" '  # noqa: S608
            f'WHERE "{column}" IS NOT NULL'
        )
        old_uuids = [r[column] for r in cur.fetchall()]

        for old_uuid in old_uuids:
            email = supabase_map.get(old_uuid)
            if not email:
                unmatched.append({"schema": schema, "table": table,
                                   "old_uuid": old_uuid, "reason": "not in auth.users"})
                continue
            new_uuid = ficium_map.get(email)
            if not new_uuid:
                unmatched.append({"schema": schema, "table": table,
                                   "old_uuid": old_uuid, "email": email,
                                   "reason": "email not in auth_portal.auth_users"})
                continue
            if old_uuid == new_uuid:
                log(f"  SKIP {schema}.{table}: {old_uuid} already correct")
                continue
            plan.append({
                "schema": schema, "table": table, "column": column,
                "old_uuid": old_uuid, "new_uuid": new_uuid, "email": email,
            })
            total_rows += 1

    # Report unmatched
    skip_emails = {e.strip().lower() for e in os.environ.get("SKIP_EMAILS", "").split(",") if e.strip()}
    blocking = [u for u in unmatched if u.get("email", "").lower() not in skip_emails]
    skipped  = [u for u in unmatched if u.get("email", "").lower() in skip_emails]

    if skipped:
        log(f"\n⏭️  Skipping {len(skipped)} user(s) via SKIP_EMAILS:")
        for u in skipped:
            log(f"  {u}")

    if blocking:
        log(f"\n⚠️  {len(blocking)} unmatched UUID(s):")
        for u in blocking:
            log(f"  {u}")
        if apply:
            log("\n❌ --apply refused: resolve unmatched users first, then re-run --dry-run.")
            conn.rollback()
            return 1
    elif unmatched and not blocking:
        log("\n✅ All unmatched users are in SKIP_EMAILS — proceeding.")

    log(f"\n{'[DRY-RUN] ' if dry_run else ''}Plan: {total_rows} row(s) to update across "
        f"{len({p['schema']+'.'+p['table'] for p in plan})} table(s)")

    if dry_run:
        for p in plan:
            log(f"  WOULD UPDATE {p['schema']}.{p['table']} "
                f"SET auth_user_id={p['new_uuid']} "
                f"WHERE auth_user_id={p['old_uuid']} (email={p['email']})")
        conn.rollback()
        log("\n✅ Dry-run complete. Run with --apply to execute.")
        return 0 if not unmatched else 1

    # ── APPLY ─────────────────────────────────────────────────────────────────
    log("\nDropping old FK constraints referencing auth.users…")
    cur.execute("""
        SELECT tc.constraint_name, tc.table_schema, tc.table_name
        FROM   information_schema.table_constraints tc
        JOIN   information_schema.referential_constraints rc
               ON rc.constraint_name = tc.constraint_name
        JOIN   information_schema.constraint_column_usage ccu
               ON ccu.constraint_name = rc.unique_constraint_name
        WHERE  tc.constraint_type = 'FOREIGN KEY'
          AND  ccu.table_schema   = 'auth'
          AND  ccu.table_name     = 'users'
          AND  tc.table_schema NOT IN ('pg_catalog','information_schema')
    """)
    fks = cur.fetchall()
    for fk in fks:
        stmt = (f'ALTER TABLE "{fk["table_schema"]}"."{fk["table_name"]}" '
                f'DROP CONSTRAINT IF EXISTS "{fk["constraint_name"]}"')
        log(f"  {stmt}")
        cur.execute(stmt)

    log("\nApplying updates…")
    for p in plan:
        # Log before write
        cur.execute("""
            INSERT INTO public._identity_migration_log
                (schema_name, table_name, column_name, old_uuid, new_uuid, email, dry_run)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (p["schema"], p["table"], p["column"],
              p["old_uuid"], p["new_uuid"], p["email"], False))

        cur.execute(
            f'UPDATE "{p["schema"]}"."{p["table"]}" '  # noqa: S608
            f'SET "{p["column"]}" = %s '
            f'WHERE "{p["column"]}" = %s',
            (p["new_uuid"], p["old_uuid"]),
        )
        log(f"  ✓ {p['schema']}.{p['table']}: {p['old_uuid']} → {p['new_uuid']} ({p['email']})")

    conn.commit()
    log(f"\n✅ Applied {total_rows} update(s). Run scripts/verify_after_apply.sql to confirm.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Identity backfill: Supabase → ficium-auth UUIDs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply",   action="store_true")
    args = parser.parse_args()
    sys.exit(run(apply=args.apply))


if __name__ == "__main__":
    main()
