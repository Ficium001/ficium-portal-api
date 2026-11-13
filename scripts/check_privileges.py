"""Guard: the API roles hold no owner-level privileges on any Ficium table.

    PORTAL_DB_DSN=... APP_DB_DSN=... python scripts/check_privileges.py

Supabase hands `anon` and `authenticated` TRUNCATE, TRIGGER, REFERENCES (and
MAINTAIN on Postgres 17+) on every new table by default. TRUNCATE ignores RLS
entirely, so a bug in any SQL function or RPC would let a caller empty a table;
the other three are owner-level operations no request ever needs. Found and
removed on Nov 12 2026 (db/049_revoke_unneeded_table_privileges.sql, and the
same migration in the ficium repo for the App DB). This keeps them removed: a
table created from the dashboard, a new migration that GRANTs too much, or a
changed default would otherwise bring them back silently.

Two checks per database, both read-only:
  1. every existing table / view / foreign table in a Ficium schema
  2. the default privileges that future tables inherit (role postgres, or any
     role but supabase_admin, whose defaults cannot be changed from our role;
     tables supabase_admin itself creates are caught by check 1 next run)

Checks every DSN that is set (PORTAL_DB_DSN, APP_DB_DSN). Exit 1, with a GitHub
error annotation per finding, when anything is found.
"""
from __future__ import annotations

import os
import sys

import psycopg2
from psycopg2.extensions import connection as Conn

ROLES = ("anon", "authenticated")
BASE_PRIVS = ("TRUNCATE", "TRIGGER", "REFERENCES")
MAINTAIN_PG = 170000  # MAINTAIN was added in Postgres 17

# Schemas owned by Supabase / Postgres, not by Ficium.
SYSTEM_SCHEMAS = (
    "pg_catalog", "information_schema", "pg_toast", "auth", "storage", "realtime",
    "vault", "extensions", "graphql", "graphql_public", "pgbouncer",
    "supabase_functions", "supabase_migrations", "cron", "net", "_realtime",
)

TABLES_SQL = """
SELECT n.nspname, c.relname, r.rolname, string_agg(p.priv, ', ' ORDER BY p.priv)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(%(roles)s::text[]) AS r(rolname)
CROSS JOIN unnest(%(privs)s::text[]) AS p(priv)
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname <> ALL (%(system)s::text[])
  AND n.nspname NOT LIKE 'pg\\_%%'
  AND EXISTS (SELECT 1 FROM pg_roles x WHERE x.rolname = r.rolname)
  AND has_table_privilege(r.rolname, c.oid, p.priv)
GROUP BY n.nspname, c.relname, r.rolname
ORDER BY 1, 2, 3
"""

DEFAULTS_SQL = """
SELECT pg_get_userbyid(d.defaclrole), coalesce(n.nspname, '(all schemas)'),
       pg_get_userbyid(a.grantee), string_agg(DISTINCT a.privilege_type, ', ')
FROM pg_default_acl d
LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
CROSS JOIN LATERAL aclexplode(d.defaclacl) AS a
WHERE d.defaclobjtype = 'r'
  AND pg_get_userbyid(a.grantee) = ANY (%(roles)s::text[])
  AND a.privilege_type = ANY (%(privs)s::text[])
  AND pg_get_userbyid(d.defaclrole) <> 'supabase_admin'
  AND (n.nspname IS NULL OR n.nspname <> ALL (%(system)s::text[]))
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""


def privileges(conn: Conn) -> tuple[str, ...]:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        version = int(cur.fetchone()[0])
    return BASE_PRIVS + (("MAINTAIN",) if version >= MAINTAIN_PG else ())


def find(conn: Conn) -> list[str]:
    """Human-readable findings; empty means clean."""
    params = {"roles": list(ROLES), "privs": list(privileges(conn)), "system": list(SYSTEM_SCHEMAS)}
    out: list[str] = []
    with conn.cursor() as cur:
        cur.execute(TABLES_SQL, params)
        for schema, table, role, privs in cur.fetchall():
            out.append(f"{schema}.{table}: {role} holds {privs}")
        cur.execute(DEFAULTS_SQL, params)
        for owner, schema, role, privs in cur.fetchall():
            out.append(f"default privileges of {owner} in {schema}: new tables give {role} {privs}")
    return out


def annotate(label: str, msg: str) -> None:
    # In GitHub Actions, surface the reason on the PR / check run itself.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=privileges::[{label}] {msg}")


def main() -> int:
    sources = (("portal", "PORTAL_DB_DSN"), ("app", "APP_DB_DSN"))
    targets = [(label, os.environ[env]) for label, env in sources if os.environ.get(env)]
    if not targets:
        sys.exit("Set PORTAL_DB_DSN and/or APP_DB_DSN")

    bad = 0
    for label, dsn in targets:
        conn = psycopg2.connect(dsn)
        conn.set_session(readonly=True, autocommit=True)
        try:
            findings = find(conn)
        finally:
            conn.close()
        if findings:
            print(f"{label} DB: {len(findings)} finding(s)")
            for f in findings:
                print(f"  {f}")
                annotate(label, f)
            bad += len(findings)
        else:
            print(f"{label} DB: clean")
    if bad:
        print(
            "\nFix: REVOKE TRUNCATE, REFERENCES, TRIGGER, MAINTAIN ON <table> "
            "FROM anon, authenticated (see db/049_revoke_unneeded_table_privileges.sql)."
        )
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as e:
        if isinstance(e.code, str):
            annotate("setup", e.code)
        raise
    except Exception as e:  # connection / SQL errors: say what, not a bare traceback
        annotate("setup", f"{type(e).__name__}: {e}")
        raise
