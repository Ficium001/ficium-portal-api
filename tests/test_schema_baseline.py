"""scripts/schema_baseline.py: normalising, role discovery, secret scan, write."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "schema_baseline", ROOT / "scripts" / "schema_baseline.py"
)
assert _spec and _spec.loader
sb = importlib.util.module_from_spec(_spec)
sys.modules["schema_baseline"] = sb
_spec.loader.exec_module(sb)

DUMP = r"""--
-- PostgreSQL database dump
--

\restrict 8f3kQZ1xY2
-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.7

SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);


CREATE SCHEMA institution;
CREATE SCHEMA public;
CREATE TABLE institution.member (id uuid NOT NULL);
CREATE POLICY member_read ON institution.member FOR SELECT TO authenticated USING (true);
CREATE POLICY svc ON institution.member TO "service_role", postgres USING (true);
GRANT USAGE ON SCHEMA institution TO authenticated;
GRANT SELECT(id) ON TABLE institution.member TO authenticated WITH GRANT OPTION;
REVOKE ALL ON TABLE institution.member FROM PUBLIC;
GRANT ALL ON SCHEMA public TO PUBLIC;
--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO anon;

--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: institution; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA institution GRANT SELECT ON TABLES TO rdr;
CREATE FUNCTION institution.f() RETURNS void LANGUAGE sql AS $$ SELECT 1 $$
    SET search_path TO 'public';

\unrestrict 8f3kQZ1xY2
"""


def test_normalise_drops_volatile_and_platform_lines() -> None:
    out = sb.normalise(DUMP)
    assert "\\restrict" not in out and "\\unrestrict" not in out
    assert "Dumped from" not in out and "Dumped by" not in out
    assert "FOR ROLE supabase_admin" not in out
    # the header of the dropped statement goes too; the one with a statement stays
    assert "Schema: public; Owner: -" not in out
    assert "Schema: institution; Owner: -" in out
    assert "CREATE SCHEMA IF NOT EXISTS public;" in out
    assert "CREATE SCHEMA institution;" in out
    assert "\n\n\n" not in out
    assert sb.normalise(out) == out  # idempotent


def test_roles_found_in_grants_policies_and_defaults() -> None:
    roles = sb.roles_in(sb.normalise(DUMP))
    assert roles == ["authenticated", "postgres", "rdr", "service_role"]


def test_scan_finds_secrets_not_sql() -> None:
    clean = sb.normalise(DUMP)
    assert sb.scan(clean) == []
    leaky = clean + (
        "-- url postgresql://postgres:hunter2@db.example.com:5432/postgres\n"
        "-- key eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig\n"
    )
    kinds = {k for _, k in sb.scan(leaky)}
    assert kinds == {"DSN with password", "JWT"}


def test_write_creates_files_and_detects_unchanged(tmp_path: Path) -> None:
    live = tmp_path / "live.sql"
    live.write_text(sb.normalise(DUMP))
    out = tmp_path / "baseline"
    assert sb.write(live, "063_x.sql", out) is True
    meta = (out / "BASELINE").read_text()
    assert "through: 063_x.sql" in meta
    assert f"schema_sha256: {sb._sha((out / 'schema.sql').read_text())}" in meta
    assert "service_role" in (out / "roles.txt").read_text()
    assert sb.write(live, "063_x.sql", out) is False  # same schema, same point
    assert sb.write(live, "064_y.sql", out) is True  # new point


def test_write_refuses_a_dump_with_secrets(tmp_path: Path) -> None:
    live = tmp_path / "live.sql"
    live.write_text(sb.normalise(DUMP) + "-- ghp_" + "a" * 36 + "\n")
    try:
        sb.write(live, "063_x.sql", tmp_path / "b")
    except SystemExit as e:
        assert "possible secrets" in str(e)
    else:
        raise AssertionError("write accepted a dump containing a token")
    assert not (tmp_path / "b").exists()
