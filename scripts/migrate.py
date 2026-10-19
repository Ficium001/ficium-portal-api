"""Apply and verify db/*.sql migrations against any Postgres.

    python scripts/migrate.py --check             # CI: fail on drift, change nothing
    python scripts/migrate.py --apply             # apply pending files in order
    python scripts/migrate.py --baseline UPTO     # record files <= UPTO as already live

DSN comes from PORTAL_DB_DSN. Each file is sent together with its ledger row
as ONE simple-query batch, which Postgres runs as a single implicit
transaction: a failed file leaves nothing behind and is not recorded. Files are
immutable once applied: an edit shows up as a hash mismatch; fix forward with
a new numbered file instead. 000_auth_shim.sql is only for non-Supabase
Postgres and is recorded as skipped when auth.uid() already exists.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extensions import connection as Conn

DB_DIR = Path(os.environ.get("MIGRATIONS_DIR") or Path(__file__).resolve().parent.parent / "db")
FILE_RE = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.sql$")
LEDGER = "039_migration_ledger.sql"


def files() -> list[Path]:
    found = sorted(p for p in DB_DIR.iterdir() if FILE_RE.match(p.name))
    names = [p.name for p in found]
    stray = [p.name for p in DB_DIR.glob("*.sql") if not FILE_RE.match(p.name)]
    if stray:
        sys.exit(f"Unrecognised file names in db/: {stray}")
    if len(set(names)) != len(names):
        sys.exit("Duplicate migration file names in db/")
    return found


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def q(conn: Conn, sql: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall() if cur.description else []


def ledger(conn: Conn) -> dict[str, tuple[str, str]]:
    if q(conn, "select to_regclass('ops.schema_migration')")[0][0] is None:
        return {}
    rows = q(conn, "select filename, sha256, status from ops.schema_migration")
    return {r[0]: (r[1], r[2]) for r in rows}


def diff(conn: Conn) -> tuple[list[Path], list[str], list[str]]:
    done = ledger(conn)
    pending = [p for p in files() if p.name not in done]
    changed = [p.name for p in files() if p.name in done and done[p.name][0] != sha(p)]
    orphaned = sorted(set(done) - {p.name for p in files()})
    return pending, changed, orphaned


def ledger_sql(p: Path, status: str) -> str:
    # Name and hash are regex-validated (FILE_RE, hex digest), safe to inline.
    return (
        "insert into ops.schema_migration (filename, sha256, status) "
        f"values ('{p.name}', '{sha(p)}', '{status}');"
    )


def has_auth_uid(conn: Conn) -> bool:
    return bool(q(conn, "select to_regprocedure('auth.uid()') is not null")[0][0])


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--baseline", metavar="UPTO")
    a = ap.parse_args()

    dsn = os.environ.get("PORTAL_DB_DSN")
    if not dsn:
        sys.exit("PORTAL_DB_DSN is not set")

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        return run(conn, a)
    finally:
        conn.close()


def run(conn: Conn, a: argparse.Namespace) -> int:
    if a.baseline:
        if not ledger(conn):
            q(conn, (DB_DIR / LEDGER).read_text() + "\n" + ledger_sql(DB_DIR / LEDGER, "applied"))
        done = ledger(conn)
        batch = []
        for p in files():
            if p.name <= a.baseline and p.name not in done:
                skip = p.name == "000_auth_shim.sql" and has_auth_uid(conn)
                batch.append(ledger_sql(p, "skipped" if skip else "baseline"))
                print(("skipped  " if skip else "baseline ") + p.name)
        if batch:
            q(conn, "\n".join(batch))
        return 0

    pending, changed, orphaned = diff(conn)
    for name in changed:
        print(f"EDITED AFTER APPLY: {name} (fix forward with a new file)")
        _annotate(f"EDITED AFTER APPLY: {name} (fix forward with a new file)")
    for name in orphaned:
        print(f"IN LEDGER, NOT IN REPO: {name}")
        _annotate(f"IN LEDGER, NOT IN REPO: {name}")

    if a.check:
        for p in pending:
            print(f"NOT APPLIED: {p.name}")
            _annotate(f"NOT APPLIED: {p.name}")
        bad = bool(pending or changed or orphaned)
        ok = f"migrations: {len(files())} files, all applied"
        print("migrations: drift found" if bad else ok)
        return 1 if bad else 0

    if changed or orphaned:
        print("Refusing to apply on top of drift.")
        return 1
    for p in pending:
        skip = p.name == "000_auth_shim.sql" and has_auth_uid(conn)
        body = "" if skip else p.read_text() + "\n"
        try:
            q(conn, body + ledger_sql(p, "skipped" if skip else "applied"))
        except psycopg2.Error as e:
            print(f"FAILED   {p.name}: {e.pgerror or e}".rstrip())
            print("Nothing from this file was kept; later files were not run.")
            return 1
        print(("skipped  " if skip else "applied  ") + p.name)
    print("up to date" if not pending else f"applied {len(pending)} file(s)")
    return 0


def _annotate(msg: str) -> None:
    # In GitHub Actions, surface the reason on the PR / check run itself.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::error title=migrations::" + msg.replace("\n", " "))


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit as e:
        if isinstance(e.code, str):
            _annotate(e.code)
        raise
    except Exception as e:  # connection / SQL errors: say what, not a bare traceback
        _annotate(f"{type(e).__name__}: {e}")
        raise
    if rc:
        _annotate("drift found; see step output (NOT APPLIED / EDITED / IN LEDGER lines)")
    raise SystemExit(rc)
