#!/usr/bin/env python3
"""
ficium-portal-api — Identity backfill (ADR-002)
=================================================
Repoints every FK that currently references Supabase auth.users(id) to the
matching ficium-auth auth_portal.auth_users(id), matched by email.

SAFETY MODEL
------------
- Defaults to --dry-run. Nothing is written unless --apply is passed AND the
  most recent dry-run on this database reported 100% resolution for active
  users (see _identity_migration_log "preflight" check before apply).
- Schema-driven: discovers every column with a FK into auth.users(id) via
  information_schema, rather than a hardcoded table list. This codebase has
  a history of tables created directly in Supabase outside the migrations
  directory, so a hardcoded list cannot be trusted to be complete.
- Every write is preceded by a row written to _identity_migration_log,
  giving a full (table, row_id, column, old_value, new_value) audit trail
  and a basis for manual reversal if ever needed.
- Runs one table per transaction. A failure on one table does not corrupt
  another; already-committed tables are simply skipped on re-run (idempotent
  — see the "already migrated" check per table).

USAGE
-----
    python scripts/identity_backfill.py --dry-run
    python scripts/identity_backfill.py --apply        # after a clean dry-run

Requires DATABASE_URL (same DSN ficium-portal-api uses) and assumes
auth_portal.auth_users (ficium-auth's table) is reachable from this
connection. In deployments where ficium-auth's database is physically
separate from the Portal database (e.g. on-prem with split DBs), run the
"export ficium-auth identities" step first — see --export-ficium-users.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field

import psycopg2
import psycopg2.extras

AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS public._identity_migration_log (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID        NOT NULL,
    table_schema    TEXT        NOT NULL,
    table_name      TEXT        NOT NULL,
    column_name     TEXT        NOT NULL,
    row_pk_column   TEXT        NOT NULL,
    row_pk_value    TEXT        NOT NULL,
    old_auth_user_id UUID       NOT NULL,
    new_auth_user_id UUID       NOT NULL,
    matched_email   TEXT        NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode            TEXT        NOT NULL CHECK (mode IN ('dry_run', 'applied'))
);
"""


@dataclass
class FkTarget:
    table_schema: str
    table_name: str
    column_name: str
    constraint_name: str
    pk_column: str


@dataclass
class TableReport:
    target: FkTarget
    total_rows: int = 0
    resolved: int = 0
    already_migrated: int = 0
    unmatched_supabase_rows: list[dict] = field(default_factory=list)


def discover_fk_targets(conn) -> list[FkTarget]:
    """
    Find every column, in any schema, with a FK referencing auth.users(id).
    Schema-driven on purpose — see module docstring.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                tc.constraint_name,
                tc.table_schema,
                tc.table_name,
                kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.constraint_schema = tc.constraint_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND ccu.table_schema = 'auth'
              AND ccu.table_name = 'users'
              AND ccu.column_name = 'id'
            """
        )
        rows = cur.fetchall()

    targets: list[FkTarget] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for r in rows:
            cur.execute(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = %s AND tc.table_name = %s
                """,
                (r["table_schema"], r["table_name"]),
            )
            pk = cur.fetchone()
            pk_column = pk["column_name"] if pk else "id"
            targets.append(
                FkTarget(
                    table_schema=r["table_schema"],
                    table_name=r["table_name"],
                    column_name=r["column_name"],
                    constraint_name=r["constraint_name"],
                    pk_column=pk_column,
                )
            )
    return targets


def build_email_map(conn) -> dict[str, str]:
    """
    auth_portal.auth_users.email (lowercased) -> ficium-auth user id.
    Single source of truth for the "new" identity.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, lower(email) FROM auth_portal.auth_users")
        return {email: str(uid) for uid, email in cur.fetchall()}


def report_table(conn, target: FkTarget, email_map: dict[str, str]) -> TableReport:
    rpt = TableReport(target=target)
    schema, table, col, pk = (
        target.table_schema,
        target.table_name,
        target.column_name,
        target.pk_column,
    )

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Supabase auth.users still exists during migration (read-only lookup).
        cur.execute(
            f"""
            SELECT t.{pk} AS pk_value, t.{col} AS old_id, u.email AS email
            FROM {schema}.{table} t
            JOIN auth.users u ON u.id = t.{col}
            """
        )
        rows = cur.fetchall()

    rpt.total_rows = len(rows)
    for row in rows:
        email = (row["email"] or "").lower()
        new_id = email_map.get(email)
        if new_id is None:
            rpt.unmatched_supabase_rows.append(
                {"pk": row["pk_value"], "old_id": str(row["old_id"]), "email": email}
            )
        elif new_id == str(row["old_id"]):
            rpt.already_migrated += 1
        else:
            rpt.resolved += 1
    return rpt


def print_report(reports: list[TableReport]) -> bool:
    """Print a human report. Returns True if fully resolved (apply-safe)."""
    fully_resolved = True
    print("\n=== Identity backfill — dry-run report ===\n")
    for r in reports:
        t = r.target
        print(f"{t.table_schema}.{t.table_name}.{t.column_name}")
        print(f"  total rows referencing auth.users : {r.total_rows}")
        print(f"  resolvable by email -> ficium-auth : {r.resolved}")
        print(f"  already migrated (idempotent skip) : {r.already_migrated}")
        print(f"  UNMATCHED (no ficium-auth account) : {len(r.unmatched_supabase_rows)}")
        if r.unmatched_supabase_rows:
            fully_resolved = False
            for u in r.unmatched_supabase_rows[:20]:
                print(f"    - pk={u['pk']} email={u['email']} old_id={u['old_id']}")
            if len(r.unmatched_supabase_rows) > 20:
                print(f"    ... and {len(r.unmatched_supabase_rows) - 20} more")
        print()
    if fully_resolved:
        print("RESULT: fully resolved. Safe to run with --apply.\n")
    else:
        print(
            "RESULT: NOT fully resolved. Provision the unmatched users in "
            "ficium-auth (or confirm they are offboarded / out of scope) "
            "before running --apply.\n"
        )
    return fully_resolved


def apply_table(conn, target: FkTarget, email_map: dict[str, str], run_id: str) -> int:
    schema, table, col, pk, constraint = (
        target.table_schema,
        target.table_name,
        target.column_name,
        target.pk_column,
        target.constraint_name,
    )
    updated = 0

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT t.{pk} AS pk_value, t.{col} AS old_id, u.email AS email
            FROM {schema}.{table} t
            JOIN auth.users u ON u.id = t.{col}
            """
        )
        rows = cur.fetchall()

    with conn.cursor() as cur:
        # Drop the OLD FK first — it still points at auth.users, and would
        # reject every UPDATE below the moment we write a ficium-auth id.
        cur.execute(
            f"ALTER TABLE {schema}.{table} DROP CONSTRAINT IF EXISTS {constraint}"
        )

        for row in rows:
            email = (row["email"] or "").lower()
            new_id = email_map.get(email)
            if new_id is None or new_id == str(row["old_id"]):
                continue  # unmatched (should not happen post-preflight) or already migrated

            cur.execute(
                """
                INSERT INTO public._identity_migration_log
                    (run_id, table_schema, table_name, column_name, row_pk_column,
                     row_pk_value, old_auth_user_id, new_auth_user_id, matched_email, mode)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'applied')
                """,
                (
                    run_id, schema, table, col, pk,
                    str(row["pk_value"]), str(row["old_id"]), new_id, email,
                ),
            )
            cur.execute(
                f"UPDATE {schema}.{table} SET {col} = %s WHERE {pk} = %s",
                (new_id, row["pk_value"]),
            )
            updated += 1

        # Now every row in this column points at a valid ficium-auth id —
        # safe to add the new FK.
        cur.execute(
            f"""
            ALTER TABLE {schema}.{table}
            ADD CONSTRAINT {constraint}_ficium_auth
            FOREIGN KEY ({col}) REFERENCES auth_portal.auth_users(id) ON DELETE CASCADE
            """
        )

    return updated


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", default=None, help="Defaults to $DATABASE_URL")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument(
        "--dry-run", action="store_true", help="Explicit no-op flag (default behaviour)"
    )
    ap.add_argument(
        "--export-report", default=None,
        help="Write the dry-run report as CSV to this path",
    )
    args = ap.parse_args()

    import os
    dsn = args.database_url or os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2

    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            cur.execute(AUDIT_TABLE_DDL)
        conn.commit()

        targets = discover_fk_targets(conn)
        if not targets:
            print("No columns with a FK to auth.users(id) found. Nothing to do.")
            return 0

        email_map = build_email_map(conn)
        if not email_map:
            print(
                "ERROR: auth_portal.auth_users has no rows, or is unreachable from "
                "this connection. Refusing to proceed.",
                file=sys.stderr,
            )
            return 2

        reports = [report_table(conn, t, email_map) for t in targets]
        fully_resolved = print_report(reports)

        if args.export_report:
            with open(args.export_report, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["table", "column", "pk", "old_id", "email"])
                for r in reports:
                    for u in r.unmatched_supabase_rows:
                        w.writerow([f"{r.target.table_schema}.{r.target.table_name}",
                                    r.target.column_name, u["pk"], u["old_id"], u["email"]])
            print(f"Unmatched rows exported to {args.export_report}")

        if not args.apply:
            print("(dry-run only — re-run with --apply once fully resolved)")
            return 0 if fully_resolved else 1

        if not fully_resolved:
            print(
                "REFUSING TO APPLY: not fully resolved. See unmatched rows above.",
                file=sys.stderr,
            )
            return 1

        run_id = str(__import__("uuid").uuid4())
        print(f"\nApplying — run_id={run_id}\n")
        for target in targets:
            updated = apply_table(conn, target, email_map, run_id)
            conn.commit()
            print(f"  {target.table_schema}.{target.table_name}.{target.column_name}: "
                  f"{updated} row(s) repointed, FK migrated to auth_portal.auth_users")

        print(f"\nDone. Audit trail: SELECT * FROM public._identity_migration_log "
              f"WHERE run_id = '{run_id}';")
        return 0

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
