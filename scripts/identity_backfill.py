#!/usr/bin/env python3
"""
scripts/identity_backfill.py
Repoints tenant table auth_user_id FKs from Supabase UUIDs to ficium-auth UUIDs.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]

def connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def log(msg):
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def ensure_log_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public._identity_migration_log (
            id bigserial PRIMARY KEY, run_at timestamptz DEFAULT now(),
            schema_name text NOT NULL, table_name text NOT NULL,
            column_name text NOT NULL, old_uuid uuid NOT NULL,
            new_uuid uuid NOT NULL, email text NOT NULL, dry_run boolean NOT NULL
        )
    """)

def discover_tenant_columns(cur):
    cur.execute("""
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE column_name = 'auth_user_id'
          AND table_schema NOT IN ('pg_catalog','information_schema','pg_toast','auth')
        ORDER BY table_schema, table_name
    """)
    return [{"schema": r["table_schema"], "table": r["table_name"], "column": r["column_name"]}
            for r in cur.fetchall()]

def build_email_map(cur):
    cur.execute("SELECT id::text, lower(email) AS email FROM auth_portal.auth_users")
    return {r["email"]: r["id"] for r in cur.fetchall()}

def build_supabase_email_map(cur):
    try:
        cur.execute("SELECT id::text, lower(email) AS email FROM auth.users")
        return {r["id"]: r["email"] for r in cur.fetchall()}
    except psycopg2.errors.UndefinedTable:
        cur.connection.rollback()
        return {}

def drop_auth_user_id_fks(cur):
    """Drop ALL FK constraints on any auth_user_id column across tenant schemas."""
    cur.execute("""
        SELECT tc.constraint_name, tc.table_schema, tc.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
             ON kcu.constraint_name = tc.constraint_name
            AND kcu.table_schema    = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND kcu.column_name    = 'auth_user_id'
          AND tc.table_schema NOT IN ('pg_catalog','information_schema','auth')
    """)
    fks = cur.fetchall()
    if not fks:
        log("  No auth_user_id FK constraints found.")
        return
    for fk in fks:
        stmt = (
            f'ALTER TABLE "{fk["table_schema"]}"."{fk["table_name"]}" '
            f'DROP CONSTRAINT IF EXISTS "{fk["constraint_name"]}"'
        )
        log(f"  {stmt}")
        cur.execute(stmt)

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
    ficium_map   = build_email_map(cur)
    supabase_map = build_supabase_email_map(cur)
    log(f"  ficium-auth users: {len(ficium_map)}")
    log(f"  supabase auth.users: {len(supabase_map)}")

    total_rows = 0
    unmatched: list[dict] = []
    plan: list[dict] = []

    for col in columns:
        schema, table, column = col["schema"], col["table"], col["column"]
        cur.execute(
            f'SELECT DISTINCT "{column}"::text AS uid '
            f'FROM "{schema}"."{table}" WHERE "{column}" IS NOT NULL'
        )
        for row in cur.fetchall():
            old_uuid = row["uid"]
            email = supabase_map.get(old_uuid)
            if not email:
                unmatched.append({
                    "schema": schema, "table": table,
                    "old_uuid": old_uuid, "reason": "not in auth.users",
                })
                continue
            new_uuid = ficium_map.get(email)
            if not new_uuid:
                unmatched.append({
                    "schema": schema, "table": table, "old_uuid": old_uuid,
                    "email": email, "reason": "email not in auth_portal.auth_users",
                })
                continue
            if old_uuid == new_uuid:
                log(f"  SKIP {schema}.{table}: {old_uuid} already correct")
                continue
            plan.append({"schema": schema, "table": table, "column": column,
                         "old_uuid": old_uuid, "new_uuid": new_uuid, "email": email})
            total_rows += 1

    skip_emails = {
        e.strip().lower() for e in os.environ.get("SKIP_EMAILS", "").split(",") if e.strip()
    }
    blocking = [u for u in unmatched if u.get("email","").lower() not in skip_emails]
    skipped  = [u for u in unmatched if u.get("email","").lower() in skip_emails]

    if skipped:
        log(f"\n⏭️  Skipping {len(skipped)} user(s) via SKIP_EMAILS:")
        for u in skipped:
            log(f"  {u}")

    if blocking:
        log(f"\n⚠️  {len(blocking)} unmatched UUID(s):")
        for u in blocking:
            log(f"  {u}")
        if apply:
            log("\n❌ --apply refused: resolve unmatched users first.")
            conn.rollback()
            return 1
    elif unmatched:
        log("\n✅ All unmatched users are in SKIP_EMAILS — proceeding.")

    n_tables = len({p["schema"] + "." + p["table"] for p in plan})
    prefix = "[DRY-RUN] " if dry_run else ""
    log(f"\n{prefix}Plan: {total_rows} row(s) to update across {n_tables} table(s)")

    if dry_run:
        for p in plan:
            log(
                f"  WOULD UPDATE {p['schema']}.{p['table']} "
                f"SET auth_user_id={p['new_uuid']} "
                f"WHERE auth_user_id={p['old_uuid']} (email={p['email']})"
            )
        conn.rollback()
        log("\n✅ Dry-run complete. Run with --apply to execute.")
        return 0 if not blocking else 1

    # ── APPLY ──────────────────────────────────────────────────────────────────
    log("\nDropping ALL auth_user_id FK constraints…")
    drop_auth_user_id_fks(cur)

    log("\nApplying updates…")
    for p in plan:
        cur.execute("""
            INSERT INTO public._identity_migration_log
                (schema_name, table_name, column_name, old_uuid, new_uuid, email, dry_run)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            p["schema"], p["table"], p["column"],
            p["old_uuid"], p["new_uuid"], p["email"], False,
        ))
        cur.execute(
            f'UPDATE "{p["schema"]}"."{p["table"]}" '
            f'SET "{p["column"]}" = %s WHERE "{p["column"]}" = %s',
            (p["new_uuid"], p["old_uuid"]),
        )
        log(f"  ✓ {p['schema']}.{p['table']}: {p['old_uuid']} → {p['new_uuid']} ({p['email']})")

    conn.commit()
    log(f"\n✅ Applied {total_rows} update(s). Run scripts/verify_after_apply.sql to confirm.")
    return 0

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply",   action="store_true")
    args = parser.parse_args()
    sys.exit(run(apply=args.apply))

if __name__ == "__main__":
    main()
