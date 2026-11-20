"""Apply and verify db/*.sql migrations on one database or a fleet of bank cells.

One database (unchanged behaviour, DSN from PORTAL_DB_DSN):

    python scripts/migrate.py --check             # CI: fail on drift, change nothing
    python scripts/migrate.py --apply             # apply pending files in order
    python scripts/migrate.py --baseline UPTO     # record files <= UPTO as already live
    python scripts/migrate.py --status [--json]   # what each database is at

A fleet of cells (one database per bank, see docs in ficium-infra):

    python scripts/migrate.py --cells cells.json --check
    python scripts/migrate.py --cells cells.json --apply
    python scripts/migrate.py --cells cells.json --cell mcb --apply
    python scripts/migrate.py --cells cells.json --cell newbank --apply --fresh

cells.json lists cells in rollout order (put the internal test cell first) and
names the environment variable holding each DSN. DSNs never go in the file:

    {"cells": [{"name": "internal", "dsn_env": "CELL_INTERNAL_DSN"},
               {"name": "mcb",      "dsn_env": "CELL_MCB_DSN"}]}

Guarantees:
  * Each file is sent with its ledger row as ONE simple-query batch, which
    Postgres runs as one implicit transaction: a failed file leaves nothing
    behind and is not recorded.
  * Files are immutable once applied: an edit shows as a hash mismatch; fix
    forward with a new numbered file.
  * --apply checks EVERY cell before changing ANY: an unreachable cell, an
    edited file or a ledger row with no file stops the whole run.
  * Cells are migrated one at a time, in file order, each under an advisory
    lock. The first failure stops the rollout; later cells are not touched.
  * A database with no ledger is only migrated with --fresh and --cell, and
    only when it holds no Ficium schema (a new, empty cell). A database that
    has Ficium schemas but no ledger must be baselined first.

000_auth_shim.sql is only for non-Supabase Postgres and is recorded as
skipped when auth.uid() already exists.

Use a session-mode connection (Supabase session pooler, port 5432, or a
direct connection): the advisory lock does not survive a transaction pooler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg2
from psycopg2.extensions import connection as Conn

DEFAULT_DB_DIR = Path(__file__).resolve().parent.parent / "db"
FILE_RE = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.sql$")
CELL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,79}$")
LEDGER = "039_migration_ledger.sql"
AUTH_SHIM = "000_auth_shim.sql"
# Any of these present means the database already holds Ficium data.
FICIUM_SCHEMAS = ("institution", "marketplace", "governance", "integration", "catalog")
# pg_try_advisory_lock key: fixed, so every runner on a database contends for it.
LOCK_KEY = 0x0F1C1A6D


class ConfigError(Exception):
    """Bad invocation or cells file: exit 2, nothing touched."""


@dataclass
class Cell:
    name: str
    dsn: str


@dataclass
class State:
    cell: str
    has_ledger: bool = False
    has_ficium_schema: bool = False
    pending: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    latest: str | None = None
    applied: int = 0

    @property
    def drift(self) -> bool:
        return bool(self.pending or self.edited or self.orphaned)

    def as_dict(self) -> dict[str, object]:
        return {
            "cell": self.cell,
            "has_ledger": self.has_ledger,
            "latest": self.latest,
            "applied": self.applied,
            "pending": self.pending,
            "edited": self.edited,
            "orphaned": self.orphaned,
        }


# ── Migration files ──────────────────────────────────────────────────────────


def files(db_dir: Path) -> list[Path]:
    found = sorted(p for p in db_dir.iterdir() if FILE_RE.match(p.name))
    stray = [p.name for p in db_dir.glob("*.sql") if not FILE_RE.match(p.name)]
    if stray:
        raise ConfigError(f"Unrecognised file names in {db_dir}: {stray}")
    if not any(p.name == LEDGER for p in found):
        raise ConfigError(f"{LEDGER} missing from {db_dir}")
    return found


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ledger_sql(p: Path, status: str) -> str:
    # Name and hash are regex-validated (FILE_RE, hex digest), safe to inline.
    return (
        "insert into ops.schema_migration (filename, sha256, status) "
        f"values ('{p.name}', '{sha(p)}', '{status}');"
    )


# ── Cells ────────────────────────────────────────────────────────────────────


def load_cells(path: str | None, only: str | None, env: dict[str, str]) -> list[Cell]:
    if not path:
        if only:
            raise ConfigError("--cell needs --cells (or MIGRATE_CELLS)")
        dsn = env.get("PORTAL_DB_DSN")
        if not dsn:
            raise ConfigError("PORTAL_DB_DSN is not set")
        return [Cell("portal", dsn)]

    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read cells file {path}: {e}") from e
    entries = doc.get("cells") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ConfigError('cells file needs a non-empty "cells" list')

    cells: list[Cell] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"name", "dsn_env"}:
            raise ConfigError(
                f"cells[{i}]: exactly the keys name and dsn_env "
                "(connection strings never go in this file)"
            )
        name, var = entry["name"], entry["dsn_env"]
        if not isinstance(name, str) or not CELL_NAME_RE.match(name):
            raise ConfigError(f"cells[{i}]: invalid name {name!r}")
        if not isinstance(var, str) or not ENV_NAME_RE.match(var):
            raise ConfigError(f"cells[{i}]: invalid dsn_env {var!r}")
        if name in seen:
            raise ConfigError(f"cells[{i}]: duplicate name {name!r}")
        seen.add(name)
        if only and name != only:
            continue
        dsn = env.get(var)
        if not dsn:
            raise ConfigError(f"cell {name}: {var} is not set")
        cells.append(Cell(name, dsn))
    if only and not cells:
        raise ConfigError(f"no cell named {only!r} in {path}")
    return cells


# ── Database helpers ─────────────────────────────────────────────────────────


def q(conn: Conn, sql: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall() if cur.description else []


def connect(dsn: str) -> Conn:
    conn = psycopg2.connect(dsn, connect_timeout=10, application_name="ficium-migrate")
    conn.autocommit = True
    return conn


def ledger(conn: Conn) -> dict[str, tuple[str, str]] | None:
    if q(conn, "select to_regclass('ops.schema_migration')")[0][0] is None:
        return None
    rows = q(conn, "select filename, sha256, status from ops.schema_migration")
    return {r[0]: (r[1], r[2]) for r in rows}


def has_ficium_schema(conn: Conn) -> bool:
    names = ",".join(f"'{s}'" for s in FICIUM_SCHEMAS)
    return bool(
        q(conn, f"select exists (select 1 from pg_namespace where nspname in ({names}))")[0][0]
    )


def has_auth_uid(conn: Conn) -> bool:
    return bool(q(conn, "select to_regprocedure('auth.uid()') is not null")[0][0])


def inspect(cell: str, conn: Conn, db_dir: Path) -> State:
    st = State(cell)
    done = ledger(conn)
    st.has_ledger = done is not None
    st.has_ficium_schema = has_ficium_schema(conn)
    done = done or {}
    repo = files(db_dir)
    st.pending = [p.name for p in repo if p.name not in done]
    st.edited = [p.name for p in repo if p.name in done and done[p.name][0] != sha(p)]
    st.orphaned = sorted(set(done) - {p.name for p in repo})
    st.applied = len(done)
    st.latest = max(done) if done else None
    return st


def try_lock(conn: Conn) -> bool:
    return bool(q(conn, f"select pg_try_advisory_lock({LOCK_KEY})")[0][0])


def unlock(conn: Conn) -> None:
    q(conn, f"select pg_advisory_unlock({LOCK_KEY})")


# ── Output ───────────────────────────────────────────────────────────────────


def say(msg: str, *, error: bool = False) -> None:
    print(msg)
    # In GitHub Actions, surface the reason on the PR / check run itself.
    if error and os.environ.get("GITHUB_ACTIONS") == "true":
        print("::error title=migrations::" + msg.replace("\n", " "))


def report(st: State, *, pending_is_error: bool) -> None:
    for n in st.edited:
        say(f"[{st.cell}] EDITED AFTER APPLY: {n} (fix forward with a new file)", error=True)
    for n in st.orphaned:
        say(f"[{st.cell}] IN LEDGER, NOT IN REPO: {n}", error=True)
    for n in st.pending:
        say(f"[{st.cell}] NOT APPLIED: {n}", error=pending_is_error)


# ── Commands ─────────────────────────────────────────────────────────────────


def cmd_check(conns: dict[str, Conn], db_dir: Path) -> int:
    bad = False
    for name, conn in conns.items():
        st = inspect(name, conn, db_dir)
        report(st, pending_is_error=True)
        if st.drift:
            bad = True
        else:
            print(f"[{name}] {st.applied} files, all applied")
    print("migrations: drift found" if bad else "migrations: every database up to date")
    return 1 if bad else 0


def cmd_status(conns: dict[str, Conn], db_dir: Path, as_json: bool) -> int:
    states = [inspect(n, c, db_dir) for n, c in conns.items()]
    if as_json:
        print(json.dumps([s.as_dict() for s in states], indent=2))
    else:
        for s in states:
            flag = "drift" if s.drift else "ok"
            print(
                f"[{s.cell}] {flag}: latest={s.latest} applied={s.applied} "
                f"pending={len(s.pending)} edited={len(s.edited)} orphaned={len(s.orphaned)}"
            )
    return 1 if any(s.drift for s in states) else 0


def cmd_baseline(conn: Conn, cell: str, upto: str, db_dir: Path) -> int:
    if ledger(conn) is None:
        p = db_dir / LEDGER
        q(conn, p.read_text() + "\n" + ledger_sql(p, "applied"))
    done = ledger(conn) or {}
    batch = []
    for p in files(db_dir):
        if p.name <= upto and p.name not in done:
            skip = p.name == AUTH_SHIM and has_auth_uid(conn)
            batch.append(ledger_sql(p, "skipped" if skip else "baseline"))
            print(f"[{cell}] " + ("skipped  " if skip else "baseline ") + p.name)
    if batch:
        q(conn, "\n".join(batch))
    return 0


def apply_cell(name: str, conn: Conn, db_dir: Path) -> tuple[bool, int]:
    """Apply pending files on one cell. Returns (ok, files applied)."""
    if ledger(conn) is None:
        # A fresh cell: create the ledger first (039 is idempotent and is
        # re-run and recorded in its turn) so every file, 000 onwards, can
        # record itself in the same transaction as its change.
        q(conn, (db_dir / LEDGER).read_text())
    done = ledger(conn) or {}
    count = 0
    for p in files(db_dir):
        if p.name in done:
            continue
        skip = p.name == AUTH_SHIM and has_auth_uid(conn)
        body = "" if skip else p.read_text() + "\n"
        try:
            q(conn, body + ledger_sql(p, "skipped" if skip else "applied"))
        except psycopg2.Error as e:
            say(f"[{name}] FAILED {p.name}: {(e.pgerror or str(e)).strip()}", error=True)
            print(f"[{name}] nothing from {p.name} was kept; later files were not run")
            return False, count
        count += 1
        print(f"[{name}] " + ("skipped  " if skip else "applied  ") + p.name)
    return True, count


def cmd_apply(conns: dict[str, Conn], db_dir: Path, fresh: bool, single_cell: bool) -> int:
    # 1. Preflight every cell before touching any.
    states = {n: inspect(n, c, db_dir) for n, c in conns.items()}
    blocked = False
    for st in states.values():
        report(st, pending_is_error=False)
        if st.edited or st.orphaned:
            blocked = True
        if not st.has_ledger:
            if st.has_ficium_schema:
                say(
                    f"[{st.cell}] has Ficium schemas but no ledger: run --baseline first",
                    error=True,
                )
                blocked = True
            elif not (fresh and single_cell):
                say(
                    f"[{st.cell}] has no ledger: a new cell is migrated only with "
                    "--cell NAME --fresh",
                    error=True,
                )
                blocked = True
    if blocked:
        say("Refusing to apply: nothing was changed on any database.", error=True)
        return 1
    if fresh and any(st.has_ledger for st in states.values()):
        say("--fresh is only for a database with no ledger.", error=True)
        return 1

    # 2. Roll out one cell at a time, in order. Stop at the first failure.
    names = list(conns)
    for i, name in enumerate(names):
        conn = conns[name]
        if not states[name].pending:
            print(f"[{name}] up to date")
            continue
        if not try_lock(conn):
            say(f"[{name}] another migration holds the lock; stopping", error=True)
            _stopped(names[i + 1 :])
            return 1
        try:
            ok, n = apply_cell(name, conn, db_dir)
        finally:
            unlock(conn)
        if not ok:
            _stopped(names[i + 1 :])
            return 1
        print(f"[{name}] applied {n} file(s)")
    print("migrations: every database up to date")
    return 0


def _stopped(rest: list[str]) -> None:
    for n in rest:
        say(f"[{n}] not started (rollout stopped)", error=True)


# ── Entry point ──────────────────────────────────────────────────────────────


def parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="fail on any drift, change nothing")
    g.add_argument("--apply", action="store_true", help="apply pending files, cell by cell")
    g.add_argument(
        "--baseline", metavar="UPTO", help="record files <= UPTO as already live (one cell)"
    )
    g.add_argument("--status", action="store_true", help="report where each database is")
    ap.add_argument("--cells", default=os.environ.get("MIGRATE_CELLS"), help="cells JSON file")
    ap.add_argument("--cell", help="only this cell")
    ap.add_argument("--fresh", action="store_true", help="allow a new, empty cell (with --cell)")
    ap.add_argument("--json", action="store_true", help="with --status: JSON output")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    a = parse(argv)
    env = dict(os.environ) if env is None else env
    db_dir = Path(env.get("MIGRATIONS_DIR") or DEFAULT_DB_DIR)
    try:
        if a.fresh and not (a.apply and (a.cell or not a.cells)):
            raise ConfigError("--fresh needs --apply and, with --cells, --cell NAME")
        if a.json and not a.status:
            raise ConfigError("--json needs --status")
        cells = load_cells(a.cells, a.cell, env)
        if a.baseline and len(cells) != 1:
            raise ConfigError("--baseline runs on one cell: add --cell NAME")
        files(db_dir)  # validate the directory before connecting anywhere
    except ConfigError as e:
        say(f"config: {e}", error=True)
        return 2

    conns: dict[str, Conn] = {}
    try:
        for c in cells:
            try:
                conns[c.name] = connect(c.dsn)
            except psycopg2.Error as e:
                say(f"[{c.name}] cannot connect: {str(e).strip()}", error=True)
                say("Nothing was changed on any database.", error=True)
                return 1
        if a.check:
            return cmd_check(conns, db_dir)
        if a.status:
            return cmd_status(conns, db_dir, a.json)
        if a.baseline:
            ((name, conn),) = conns.items()
            return cmd_baseline(conn, name, a.baseline, db_dir)
        single = a.cell is not None or not a.cells
        return cmd_apply(conns, db_dir, a.fresh, single)
    finally:
        for conn in conns.values():
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
