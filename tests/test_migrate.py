"""scripts/migrate.py against real Postgres: one database and a fleet of cells.

Needs MIGRATE_TEST_PG, a superuser DSN to a scratch server (CI starts one).
Each test creates its own databases and drops them afterwards. Outside CI the
module is skipped when the variable is unset; in CI a missing variable fails,
so this suite can never go green by not running.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import make_dsn, parse_dsn

ROOT = Path(__file__).resolve().parent.parent
ADMIN_DSN = os.environ.get("MIGRATE_TEST_PG", "")

if not ADMIN_DSN:
    if os.environ.get("CI"):
        raise RuntimeError(
            "MIGRATE_TEST_PG is not set in CI: the migration runner tests would not run"
        )
    pytest.skip("MIGRATE_TEST_PG not set", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("migrate", ROOT / "scripts" / "migrate.py")
assert _spec and _spec.loader
migrate = importlib.util.module_from_spec(_spec)
sys.modules["migrate"] = migrate  # dataclasses resolve their module by name
_spec.loader.exec_module(migrate)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _admin() -> psycopg2.extensions.connection:
    c = psycopg2.connect(ADMIN_DSN)
    c.autocommit = True
    return c


@pytest.fixture
def make_db() -> Iterator:
    created: list[str] = []

    def _make() -> str:
        name = "mig_" + uuid.uuid4().hex[:12]
        a = _admin()  # not `with a:` - that opens a transaction, and CREATE DATABASE refuses one
        try:
            with a.cursor() as cur:
                cur.execute(f'create database "{name}"')
        finally:
            a.close()
        created.append(name)
        return make_dsn(**{**parse_dsn(ADMIN_DSN), "dbname": name})

    yield _make
    a = _admin()
    with a.cursor() as cur:
        for name in created:
            cur.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s and pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'drop database if exists "{name}"')
    a.close()


@pytest.fixture
def mig_dir(tmp_path: Path) -> Path:
    """A small migrations folder: two files, the real ledger file, one more."""
    d = tmp_path / "db"
    d.mkdir()
    (d / "001_alpha.sql").write_text(
        "create schema institution; create table institution.alpha (id int);\n"
    )
    (d / "002_beta.sql").write_text("create table institution.beta (id int);\n")
    shutil.copy(ROOT / "db" / migrate.LEDGER, d / migrate.LEDGER)
    (d / "040_gamma.sql").write_text("create table institution.gamma (id int);\n")
    return d


def run(args: list[str], mig_dir: Path, **env: str) -> int:
    return int(migrate.main(args, {"MIGRATIONS_DIR": str(mig_dir), **env}))


def cells_file(tmp_path: Path, names: list[str]) -> str:
    p = tmp_path / "cells.json"
    p.write_text(
        json.dumps(
            {
                "cells": [
                    {"name": n, "dsn_env": f"CELL_{n.upper().replace('-', '_')}_DSN"} for n in names
                ]
            }
        )
    )
    return str(p)


def cell_env(**dsns: str) -> dict[str, str]:
    return {f"CELL_{k.upper()}_DSN": v for k, v in dsns.items()}


def rows(dsn: str, sql: str) -> list[tuple]:
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def ledger_names(dsn: str) -> list[str]:
    return [r[0] for r in rows(dsn, "select filename from ops.schema_migration order by filename")]


def table_exists(dsn: str, name: str) -> bool:
    return bool(rows(dsn, f"select to_regclass('{name}') is not null")[0][0])


def fresh_cell(make_db, mig_dir: Path, tmp_path: Path, name: str) -> str:
    dsn = make_db()
    path = cells_file(tmp_path, [name])
    assert (
        run(
            ["--cells", path, "--cell", name, "--apply", "--fresh"],
            mig_dir,
            **cell_env(**{name: dsn}),
        )
        == 0
    )
    return dsn


# ── New cells ────────────────────────────────────────────────────────────────


def test_new_cell_refused_without_fresh(make_db, mig_dir, tmp_path):
    dsn = make_db()
    path = cells_file(tmp_path, ["bank"])
    assert run(["--cells", path, "--cell", "bank", "--apply"], mig_dir, **cell_env(bank=dsn)) == 1
    assert not table_exists(dsn, "ops.schema_migration")


def test_new_cell_built_from_nothing_with_fresh(make_db, mig_dir, tmp_path):
    dsn = fresh_cell(make_db, mig_dir, tmp_path, "bank")
    assert ledger_names(dsn) == ["001_alpha.sql", "002_beta.sql", migrate.LEDGER, "040_gamma.sql"]
    assert table_exists(dsn, "institution.gamma")


def test_fresh_refused_for_whole_fleet(make_db, mig_dir, tmp_path):
    path = cells_file(tmp_path, ["a", "b"])
    env = cell_env(a=make_db(), b=make_db())
    assert run(["--cells", path, "--apply", "--fresh"], mig_dir, **env) == 2


def test_unledgered_database_with_ficium_schema_never_migrated(make_db, mig_dir, tmp_path):
    dsn = make_db()
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute("create schema institution")
    path = cells_file(tmp_path, ["live"])
    assert (
        run(
            ["--cells", path, "--cell", "live", "--apply", "--fresh"], mig_dir, **cell_env(live=dsn)
        )
        == 1
    )
    assert not table_exists(dsn, "institution.alpha")


def test_fresh_refused_when_ledger_exists(make_db, mig_dir, tmp_path):
    dsn = fresh_cell(make_db, mig_dir, tmp_path, "bank")
    path = cells_file(tmp_path, ["bank"])
    assert (
        run(
            ["--cells", path, "--cell", "bank", "--apply", "--fresh"], mig_dir, **cell_env(bank=dsn)
        )
        == 1
    )


# ── Fleet rollout ────────────────────────────────────────────────────────────


def test_rollout_applies_new_file_to_every_cell(make_db, mig_dir, tmp_path):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    b = fresh_cell(make_db, mig_dir, tmp_path, "b")
    (mig_dir / "041_delta.sql").write_text("create table institution.delta (id int);\n")
    path = cells_file(tmp_path, ["a", "b"])
    env = cell_env(a=a, b=b)
    assert run(["--cells", path, "--check"], mig_dir, **env) == 1
    assert run(["--cells", path, "--apply"], mig_dir, **env) == 0
    assert run(["--cells", path, "--check"], mig_dir, **env) == 0
    assert table_exists(a, "institution.delta") and table_exists(b, "institution.delta")


def test_first_failure_stops_rollout_and_leaves_nothing_behind(make_db, mig_dir, tmp_path):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    b = fresh_cell(make_db, mig_dir, tmp_path, "b")
    c = fresh_cell(make_db, mig_dir, tmp_path, "c")
    # Cell b already has a table the new file creates: the file must fail there.
    with psycopg2.connect(b) as conn, conn.cursor() as cur:
        cur.execute("create table institution.delta (id int)")
    (mig_dir / "041_delta.sql").write_text(
        "create table institution.epsilon (id int);\ncreate table institution.delta (id int);\n"
    )
    path = cells_file(tmp_path, ["a", "b", "c"])
    assert run(["--cells", path, "--apply"], mig_dir, **cell_env(a=a, b=b, c=c)) == 1
    assert "041_delta.sql" in ledger_names(a)
    # b: the whole file rolled back, including the statement that succeeded.
    assert "041_delta.sql" not in ledger_names(b)
    assert not table_exists(b, "institution.epsilon")
    # c: never started.
    assert "041_delta.sql" not in ledger_names(c)
    assert not table_exists(c, "institution.epsilon")


def test_edited_file_on_any_cell_blocks_every_cell(make_db, mig_dir, tmp_path):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    b = fresh_cell(make_db, mig_dir, tmp_path, "b")
    with psycopg2.connect(b) as conn, conn.cursor() as cur:
        cur.execute(
            "update ops.schema_migration set sha256 = repeat('0', 64) "
            "where filename = '002_beta.sql'"
        )
    (mig_dir / "041_delta.sql").write_text("create table institution.delta (id int);\n")
    path = cells_file(tmp_path, ["a", "b"])
    assert run(["--cells", path, "--apply"], mig_dir, **cell_env(a=a, b=b)) == 1
    assert not table_exists(a, "institution.delta")  # a is first, and still untouched


def test_orphaned_ledger_row_blocks_rollout(make_db, mig_dir, tmp_path):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    with psycopg2.connect(a) as conn, conn.cursor() as cur:
        cur.execute(
            "insert into ops.schema_migration (filename, sha256) "
            "values ('099_gone.sql', repeat('a', 64))"
        )
    (mig_dir / "041_delta.sql").write_text("create table institution.delta (id int);\n")
    path = cells_file(tmp_path, ["a"])
    assert run(["--cells", path, "--apply"], mig_dir, **cell_env(a=a)) == 1
    assert not table_exists(a, "institution.delta")


def test_locked_cell_stops_rollout(make_db, mig_dir, tmp_path):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    b = fresh_cell(make_db, mig_dir, tmp_path, "b")
    (mig_dir / "041_delta.sql").write_text("create table institution.delta (id int);\n")
    holder = psycopg2.connect(a)
    holder.autocommit = True
    try:
        with holder.cursor() as cur:
            cur.execute(f"select pg_advisory_lock({migrate.LOCK_KEY})")
        path = cells_file(tmp_path, ["a", "b"])
        assert run(["--cells", path, "--apply"], mig_dir, **cell_env(a=a, b=b)) == 1
    finally:
        holder.close()
    assert not table_exists(a, "institution.delta")
    assert not table_exists(b, "institution.delta")


def test_unreachable_cell_blocks_before_any_change(make_db, mig_dir, tmp_path):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    (mig_dir / "041_delta.sql").write_text("create table institution.delta (id int);\n")
    bad = make_dsn(**{**parse_dsn(ADMIN_DSN), "dbname": "does_not_exist_" + uuid.uuid4().hex[:6]})
    path = cells_file(tmp_path, ["a", "b"])
    assert run(["--cells", path, "--apply"], mig_dir, **cell_env(a=a, b=bad)) == 1
    assert not table_exists(a, "institution.delta")


def test_single_cell_selection_leaves_others_alone(make_db, mig_dir, tmp_path):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    b = fresh_cell(make_db, mig_dir, tmp_path, "b")
    (mig_dir / "041_delta.sql").write_text("create table institution.delta (id int);\n")
    path = cells_file(tmp_path, ["a", "b"])
    assert run(["--cells", path, "--cell", "a", "--apply"], mig_dir, **cell_env(a=a, b=b)) == 0
    assert table_exists(a, "institution.delta")
    assert not table_exists(b, "institution.delta")


# ── Status, baseline, single database ────────────────────────────────────────


def test_status_json(make_db, mig_dir, tmp_path, capsys):
    a = fresh_cell(make_db, mig_dir, tmp_path, "a")
    (mig_dir / "041_delta.sql").write_text("create table institution.delta (id int);\n")
    path = cells_file(tmp_path, ["a"])
    capsys.readouterr()
    assert run(["--cells", path, "--status", "--json"], mig_dir, **cell_env(a=a)) == 1
    (st,) = json.loads(capsys.readouterr().out)
    assert st["cell"] == "a" and st["latest"] == "040_gamma.sql"
    assert st["pending"] == ["041_delta.sql"] and st["applied"] == 4


def test_baseline_needs_one_cell(make_db, mig_dir, tmp_path):
    path = cells_file(tmp_path, ["a", "b"])
    assert (
        run(
            ["--cells", path, "--baseline", "002_beta.sql"],
            mig_dir,
            **cell_env(a=make_db(), b=make_db()),
        )
        == 2
    )


def test_baseline_then_apply_on_existing_database(make_db, mig_dir):
    dsn = make_db()
    with psycopg2.connect(dsn) as c, c.cursor() as cur:  # a live database from before the ledger
        cur.execute(
            "create schema institution; create table institution.alpha (id int); "
            "create table institution.beta (id int)"
        )
    assert run(["--apply"], mig_dir, PORTAL_DB_DSN=dsn) == 1  # refused: baseline first
    assert run(["--baseline", "002_beta.sql"], mig_dir, PORTAL_DB_DSN=dsn) == 0
    assert run(["--apply"], mig_dir, PORTAL_DB_DSN=dsn) == 0
    assert run(["--check"], mig_dir, PORTAL_DB_DSN=dsn) == 0
    st = dict(rows(dsn, "select filename, status from ops.schema_migration"))
    assert st["001_alpha.sql"] == "baseline" and st["040_gamma.sql"] == "applied"


def test_single_database_fresh_without_cells_file(make_db, mig_dir):
    dsn = make_db()
    assert run(["--apply", "--fresh"], mig_dir, PORTAL_DB_DSN=dsn) == 0
    assert table_exists(dsn, "institution.gamma")


# ── Cells file validation ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "doc",
    [
        {"cells": [{"name": "a", "dsn_env": "A_DSN", "dsn": "postgresql://x"}]},  # secret in file
        {
            "cells": [{"name": "a", "dsn_env": "A_DSN"}, {"name": "a", "dsn_env": "B_DSN"}]
        },  # duplicate
        {"cells": [{"name": "Bad Name", "dsn_env": "A_DSN"}]},
        {"cells": [{"name": "a", "dsn_env": "lower"}]},
        {"cells": []},
    ],
)
def test_bad_cells_file_rejected_before_connecting(tmp_path, mig_dir, doc):
    p = tmp_path / "cells.json"
    p.write_text(json.dumps(doc))
    assert run(["--cells", str(p), "--check"], mig_dir, A_DSN="x", B_DSN="y") == 2


def test_missing_dsn_variable_rejected(tmp_path, mig_dir):
    path = cells_file(tmp_path, ["a"])
    assert run(["--cells", path, "--check"], mig_dir) == 2


def test_real_db_folder_is_valid():
    names = [p.name for p in migrate.files(ROOT / "db")]
    assert migrate.LEDGER in names and names == sorted(names)


# ── Building a new cell from db/baseline/ ────────────────────────────────────

BASE_SQL = """SELECT pg_catalog.set_config('search_path', '', false);
CREATE SCHEMA institution;
CREATE TABLE institution.alpha (id integer);
CREATE TABLE institution.beta (id integer);
GRANT SELECT ON TABLE institution.alpha TO authenticated;
GRANT SELECT ON TABLE institution.beta TO ficium_test_reader;
"""


def write_baseline(mig_dir: Path, body: str = BASE_SQL, through: str = migrate.LEDGER) -> None:
    d = mig_dir / "baseline"
    d.mkdir(exist_ok=True)
    (d / "schema.sql").write_text(body)
    (d / "roles.txt").write_text("# generated\nauthenticated\nficium_test_reader\n")
    digest = migrate.sha(d / "schema.sql")
    (d / "BASELINE").write_text(f"through: {through}\nschema_sha256: {digest}\n")


def test_fresh_cell_built_from_baseline(make_db, mig_dir):
    write_baseline(mig_dir)
    # A later file with unqualified names: proves the snapshot's search_path was reset.
    (mig_dir / "041_after.sql").write_text("create table after_baseline (id int);\n")
    dsn = make_db()
    assert run(["--apply", "--fresh"], mig_dir, PORTAL_DB_DSN=dsn) == 0
    st = dict(rows(dsn, "select filename, status from ops.schema_migration"))
    assert st == {
        "001_alpha.sql": "baseline",
        "002_beta.sql": "baseline",
        migrate.LEDGER: "baseline",
        "040_gamma.sql": "applied",
        "041_after.sql": "applied",
    }
    assert table_exists(dsn, "institution.beta") and table_exists(dsn, "public.after_baseline")
    assert rows(
        dsn, "select has_table_privilege('ficium_test_reader', 'institution.beta', 'select')"
    )[0][0]
    assert rows(dsn, "select auth.uid() is null")[0][0]  # compat: auth helpers present
    assert run(["--check"], mig_dir, PORTAL_DB_DSN=dsn) == 0


def test_baseline_build_is_one_transaction(make_db, mig_dir):
    write_baseline(mig_dir, BASE_SQL + "CREATE TABLE institution.alpha (id integer);\n")
    dsn = make_db()
    assert run(["--apply", "--fresh"], mig_dir, PORTAL_DB_DSN=dsn) == 1
    assert not table_exists(dsn, "institution.beta")
    assert not table_exists(dsn, "ops.schema_migration")


def test_hand_edited_baseline_refused(make_db, mig_dir):
    write_baseline(mig_dir)
    with (mig_dir / "baseline" / "schema.sql").open("a") as f:
        f.write("-- edited\n")
    assert run(["--apply", "--fresh"], mig_dir, PORTAL_DB_DSN=make_db()) == 2


def test_upto_stops_after_named_file(make_db, mig_dir):
    write_baseline(mig_dir)
    (mig_dir / "041_after.sql").write_text("create table after_baseline (id int);\n")
    dsn = make_db()
    assert run(["--apply", "--fresh", "--upto", "040_gamma.sql"], mig_dir, PORTAL_DB_DSN=dsn) == 0
    assert "041_after.sql" not in ledger_names(dsn)
    assert run(["--apply", "--upto", "999_nope.sql"], mig_dir, PORTAL_DB_DSN=dsn) == 2
    assert run(["--apply"], mig_dir, PORTAL_DB_DSN=dsn) == 0
    assert "041_after.sql" in ledger_names(dsn)


def _server_major() -> int:
    return int(rows(ADMIN_DSN, "select current_setting('server_version_num')::int / 10000")[0][0])


def _matching_pg_dump() -> str | None:
    import re
    import subprocess

    for exe in (os.environ.get("SCHEMA_BASELINE_PG_DUMP"), shutil.which("pg_dump")):
        if not exe:
            continue
        out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
        m = re.search(r"(\d+)\.", out)
        if m and int(m.group(1)) >= _server_major():
            return exe
    return None


def test_round_trip_dump_build_dump_is_identical(make_db, mig_dir, tmp_path, monkeypatch):
    """What the schema-baseline workflow does against live, on a stand-in 'live'."""
    exe = _matching_pg_dump()
    if not exe:
        pytest.skip("no pg_dump matching the server")
    monkeypatch.setenv("SCHEMA_BASELINE_PG_DUMP", exe)
    sb_spec = importlib.util.spec_from_file_location("sb", ROOT / "scripts" / "schema_baseline.py")
    assert sb_spec and sb_spec.loader
    sb = importlib.util.module_from_spec(sb_spec)
    sys.modules["sb"] = sb
    sb_spec.loader.exec_module(sb)
    monkeypatch.setattr(sb, "SCHEMAS", ("institution", "ops"))

    live = make_db()  # a 'live' database built the old way, file by file
    with psycopg2.connect(live) as c, c.cursor() as cur:
        cur.execute(
            "do $$ begin if not exists (select 1 from pg_roles where rolname='authenticated')"
            " then create role authenticated nologin; end if; end $$"
        )
    (mig_dir / "041_policy.sql").write_text(
        "alter table institution.alpha enable row level security;\n"
        "create policy a_read on institution.alpha for select to authenticated using (true);\n"
        "grant select on institution.alpha to authenticated;\n"
    )
    assert run(["--apply", "--fresh"], mig_dir, PORTAL_DB_DSN=live) == 0
    live_sql = tmp_path / "live.sql"
    live_sql.write_text(sb.normalise(sb.pg_dump(live)))
    assert sb.write(live_sql, "041_policy.sql", mig_dir / "baseline") is True

    cell = make_db()
    assert run(["--apply", "--fresh"], mig_dir, PORTAL_DB_DSN=cell) == 0
    assert sb.normalise(sb.pg_dump(cell)) == live_sql.read_text()
    assert run(["--check"], mig_dir, PORTAL_DB_DSN=cell) == 0


def test_real_baseline_builds_a_cell(make_db):
    """The committed db/baseline/ builds a cell on plain Postgres 17 and passes --check."""
    if not (ROOT / "db" / "baseline" / "schema.sql").exists():
        pytest.skip("no committed baseline yet")
    if _server_major() < 17:
        pytest.skip("the baseline needs Postgres 17+")
    dsn = make_db()
    env = {"MIGRATIONS_DIR": str(ROOT / "db"), "PORTAL_DB_DSN": dsn}
    assert migrate.main(["--apply", "--fresh"], env) == 0
    assert migrate.main(["--check"], env) == 0
    n = rows(
        dsn,
        "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'institution' and c.relrowsecurity",
    )[0][0]
    assert n > 20, "RLS should be on across the institution schema"
