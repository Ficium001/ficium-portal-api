"""Schema baseline for new bank cells: a schema-only snapshot of the live Portal DB.

    python scripts/schema_baseline.py live-point DSN    # latest file in the live ledger
    python scripts/schema_baseline.py dump DSN OUT [--canonical ADMIN_DSN]
    python scripts/schema_baseline.py write LIVE_SQL --through FILE
    python scripts/schema_baseline.py scan FILE         # fail on anything like a secret

`write` turns a normalised dump into db/baseline/:
  schema.sql  the snapshot (header + dump); never edited by hand
  roles.txt   every role the snapshot grants to, created NOLOGIN on a new cell
  BASELINE    through (last db/ file the snapshot includes), taken, schema_sha256

Schema only: no rows from any table, so no bank or borrower data. scripts/migrate.py
--fresh builds a new cell from it. The schema-baseline workflow proves it:
it builds a scratch cell to the live ledger point and diffs its dump against
live. Any difference fails, including hand-made hotfixes on live that no
db/ file explains.

A dump of live is taken through the Supabase pooler, where pg_dump's own
search_path='' did not always hold (observed 20 Nov 2026: one dump came out
with search_path 'public, extensions' and unqualified names). --canonical
removes that variable: the dump is restored into a throwaway database on
ADMIN_DSN (a direct connection) and dumped again from there, so live and
scratch are both dumped the same way.

Every dump uses the same pinned pg_dump (the postgres:17-alpine image, through
docker), so live and scratch dumps are byte-comparable: a different pg_dump
build can emit different output for identical schemas. Set
SCHEMA_BASELINE_PG_DUMP to use a local binary instead (tests without docker).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn, parse_dsn

ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = ROOT / "db" / "baseline"

# Ficium-owned schemas on the Portal DB. Supabase-managed schemas (auth, storage,
# realtime, vault, cron, net, extensions, graphql*, supabase_migrations) and the
# retired/cleared holding schemas are deliberately left out.
SCHEMAS = (
    "admin",
    "audit",
    "auth_portal",
    "autobid",
    "billing",
    "catalog",
    "entitlements",
    "governance",
    "identity",
    "institution",
    "integration",
    "marketplace",
    "ops",
    "portal_admin",
    "public",
)
PG_IMAGE = "postgres:17-alpine"
DUMP_FLAGS = (
    "--schema-only",
    "--no-owner",
    "--no-publications",
    "--no-subscriptions",
    "--no-security-labels",
    "--no-tablespaces",
    # Monthly partitions (usage_event_2026_11, execution_2027_01, ...) are created by
    # ensure_*_partitions() on a daily schedule: data layout, not schema. Left out of
    # both dumps so the rebuild-and-compare does not drift every month. The parents
    # and their DEFAULT partitions stay in (migration 075).
    "--exclude-table=entitlements.usage_event_[0-9]*",
    "--exclude-table=autobid.execution_[0-9]*",
)

_DROP_LINE = re.compile(
    r"^(\\restrict |\\unrestrict |-- Dumped from database version|-- Dumped by pg_dump version)"
)
# Supabase platform defaults (for objects supabase_admin creates). Meaningless in a
# cell, and only a superuser can set them, so they would fail on Neon.
_SUPABASE_DEFAULTS = re.compile(r"^ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin ")

_SECRETS = {
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    "Stripe key": re.compile(r"\b[sr]k_(live|test)_[0-9A-Za-z]{10,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "AWS key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Resend key": re.compile(r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{8,}"),
    "DSN with password": re.compile(r"postgres(ql)?://[^:\s/]+:[^@\s]+@"),
}

_GRANT = re.compile(r"^(?:GRANT|REVOKE) .* (?:TO|FROM) (.+?);$")
_DEFAULTS = re.compile(r"^ALTER DEFAULT PRIVILEGES FOR ROLE (\S+) .* (?:TO|FROM) (.+?);$")
_POLICY = re.compile(r"^CREATE POLICY .*? TO (.+?)(?: USING| WITH CHECK|;|$)")
_IDENT = re.compile(r'^"?([a-z_][a-z0-9_]*)"?$')


# ── pg_dump ──────────────────────────────────────────────────────────────────


def _local_pg_dump() -> str | None:
    # Only when explicitly asked for: see the module docstring.
    return os.environ.get("SCHEMA_BASELINE_PG_DUMP") or None


def _session_dsn(dsn: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import migrate  # noqa: PLC0415 - sibling script

    return str(migrate.session_dsn(dsn))


def pg_dump(dsn: str) -> str:
    # pg_dump makes session-level settings (search_path, row_security, timeouts):
    # never through the transaction pooler, where they would outlive the dump.
    dsn = _session_dsn(dsn)
    args = list(DUMP_FLAGS) + [f"--schema={s}" for s in SCHEMAS]
    exe = _local_pg_dump()
    if exe:
        cmd, env = [exe, "--dbname", dsn, *args], None
    else:
        # The DSN goes in through the environment, never on the command line.
        script = 'exec pg_dump --dbname "$PGDUMP_DSN" "$@"'
        cmd = ["docker", "run", "--rm", "--network", "host", "-e", "PGDUMP_DSN", PG_IMAGE]
        cmd += ["sh", "-c", script, "pg_dump", *args]
        env = {**os.environ, "PGDUMP_DSN": dsn}
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        sys.exit(f"pg_dump failed: {res.stderr.strip()[:2000]}")
    return res.stdout


def _sections(lines: list[str]) -> list[list[str]]:
    """Split a dump at pg_dump's object headers ("--" / "-- Name: ..." / "--")."""
    out: list[list[str]] = [[]]
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        header = nxt.startswith("-- Name: ") and i + 2 < len(lines) and lines[i + 2] == "--"
        if line == "--" and (header or nxt.startswith("-- PostgreSQL database dump complete")):
            out.append([])
        out[-1].append(line)
    return out


def normalise(dump: str) -> str:
    kept = []
    for line in dump.splitlines():
        if _DROP_LINE.match(line) or _SUPABASE_DEFAULTS.match(line):
            continue
        if line == "CREATE SCHEMA public;":
            line = "CREATE SCHEMA IF NOT EXISTS public;"
        kept.append(line)
    out: list[str] = []
    for sec in _sections(kept):
        is_object = len(sec) >= 3 and sec[1].startswith("-- Name: ")
        body = sec[3:] if is_object else sec
        if is_object and not any(ln.strip() for ln in body):
            continue  # header of an object whose statements were all dropped
        out.extend(sec)
    text = "\n".join(out).strip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)


def canonical(dump: str, admin_dsn: str) -> str:
    """Restore `dump` into a throwaway database on `admin_dsn`, dump it again, drop it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import migrate  # noqa: PLC0415 - sibling script, same compat layer as a new cell

    name = "canon_" + secrets.token_hex(6)
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            # Database names cannot be bind parameters; `name` is random hex and is quoted by
            # sql.Identifier, psycopg2's composition API for identifiers.
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            cur.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        dsn = make_dsn(**{**parse_dsn(admin_dsn), "dbname": name})
        conn = migrate.connect(dsn)
        try:
            compat = migrate.compat_sql(conn, roles_in(dump), migrate.DEFAULT_DB_DIR)
            migrate.q(conn, compat + dump)
        finally:
            conn.close()
        return normalise(pg_dump(dsn))
    finally:
        with admin.cursor() as cur:
            cur.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s and pid <> pg_backend_pid()",
                (name,),
            )
            # Same as above: random hex, quoted by sql.Identifier.
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            cur.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(name)))
        admin.close()


# ── Roles, secrets ───────────────────────────────────────────────────────────


def _names(chunk: str) -> list[str]:
    chunk = re.sub(r" (WITH GRANT OPTION|GRANTED BY \S+|CASCADE)\b.*$", "", chunk)
    names = []
    for part in chunk.split(","):
        m = _IDENT.match(part.strip())
        if m and m.group(1) != "public":
            names.append(m.group(1))
    return names


def roles_in(dump: str) -> list[str]:
    found: set[str] = set()
    for line in dump.splitlines():
        if m := _DEFAULTS.match(line):
            found.update(_names(m.group(1)) + _names(m.group(2)))
        elif m := _GRANT.match(line):
            found.update(_names(m.group(1)))
        elif m := _POLICY.match(line):
            found.update(_names(m.group(1)))
    found.discard("PUBLIC")
    return sorted(r for r in found if r.lower() != "public")


def scan(text: str) -> list[tuple[int, str]]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in _SECRETS.items():
            if rx.search(line):
                hits.append((n, kind))
    return hits


# ── Commands ─────────────────────────────────────────────────────────────────


def live_point(dsn: str) -> str:
    conn = psycopg2.connect(
        _session_dsn(dsn), connect_timeout=20, application_name="ficium-schema-baseline"
    )
    try:
        with conn.cursor() as cur:
            cur.execute("select max(filename) from ops.schema_migration")
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        sys.exit("live ledger is empty")
    return str(row[0])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write(live_sql: Path, through: str, out_dir: Path = BASELINE_DIR) -> bool:
    """Write db/baseline/. Returns False (and writes nothing) if the snapshot is unchanged."""
    body = live_sql.read_text()
    hits = scan(body)
    if hits:
        sys.exit(f"refusing to write: possible secrets at lines {hits[:20]}")
    schema = out_dir / "schema.sql"
    header = (
        "-- Ficium Portal DB schema baseline. GENERATED: do not edit by hand.\n"
        f"-- Schema only (no rows), through {through}. Regenerate with the\n"
        "-- schema-baseline workflow on a schema-baseline/* branch; see db/README.md.\n\n"
    )
    meta_path = out_dir / "BASELINE"
    if schema.exists() and meta_path.exists():
        old_through = next(
            (
                ln.split(":", 1)[1].strip()
                for ln in meta_path.read_text().splitlines()
                if ln.startswith("through:")
            ),
            "",
        )
        if schema.read_text() == header + body and old_through == through:
            print("baseline unchanged")
            return False
    out_dir.mkdir(parents=True, exist_ok=True)
    schema.write_text(header + body)
    roles = roles_in(body)
    (out_dir / "roles.txt").write_text(
        "# Roles the baseline grants to; created NOLOGIN on a new cell. Generated.\n"
        + "\n".join(roles)
        + "\n"
    )
    taken = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_path.write_text(
        f"through: {through}\ntaken: {taken}\nschema_sha256: {_sha(header + body)}\n"
    )
    print(f"baseline written through {through}: {len(body.splitlines())} lines, roles {roles}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("live-point")
    p.add_argument("dsn")
    p = sub.add_parser("dump")
    p.add_argument("dsn")
    p.add_argument("out", type=Path)
    p.add_argument("--canonical", metavar="ADMIN_DSN", help="restore and re-dump on this server")
    p = sub.add_parser("write")
    p.add_argument("live_sql", type=Path)
    p.add_argument("--through", required=True)
    p = sub.add_parser("scan")
    p.add_argument("file", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "live-point":
        print(live_point(a.dsn))
    elif a.cmd == "dump":
        text = normalise(pg_dump(a.dsn))
        if a.canonical:
            text = canonical(text, a.canonical)
        a.out.write_text(text)
        print(f"dumped {len(a.out.read_text().splitlines())} lines to {a.out}")
    elif a.cmd == "write":
        write(a.live_sql, a.through)
    elif a.cmd == "scan":
        hits = scan(a.file.read_text())
        for n, kind in hits:
            print(f"{a.file}:{n}: possible {kind}")
        return 1 if hits else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
