"""Schema baseline for new bank cells: a schema-only snapshot of the live Portal DB.

    python scripts/schema_baseline.py live-point DSN    # latest file in the live ledger
    python scripts/schema_baseline.py dump DSN OUT      # normalised dump of the Ficium schemas
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

pg_dump must be 17+ (the server is 17). If the local pg_dump is older or
missing, the postgres:17-alpine image is used through docker.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg2

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
    # SCHEMA_BASELINE_PG_DUMP: tests point this at a pg_dump matching their server.
    if forced := os.environ.get("SCHEMA_BASELINE_PG_DUMP"):
        return forced
    exe = shutil.which("pg_dump")
    if not exe:
        return None
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.", out)
    return exe if m and int(m.group(1)) >= 17 else None


def pg_dump(dsn: str) -> str:
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


def normalise(dump: str) -> str:
    out = []
    for line in dump.splitlines():
        if _DROP_LINE.match(line) or _SUPABASE_DEFAULTS.match(line):
            continue
        if line == "CREATE SCHEMA public;":
            line = "CREATE SCHEMA IF NOT EXISTS public;"
        out.append(line)
    text = "\n".join(out).strip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)


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
    conn = psycopg2.connect(dsn, connect_timeout=20, application_name="ficium-schema-baseline")
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
    p = sub.add_parser("write")
    p.add_argument("live_sql", type=Path)
    p.add_argument("--through", required=True)
    p = sub.add_parser("scan")
    p.add_argument("file", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "live-point":
        print(live_point(a.dsn))
    elif a.cmd == "dump":
        a.out.write_text(normalise(pg_dump(a.dsn)))
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
