"""scripts/migrate.py session_dsn / describe_dsn: session state never goes through 6543."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from psycopg2.extensions import parse_dsn

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("migrate_dsn", ROOT / "scripts" / "migrate.py")
assert _spec and _spec.loader
migrate = importlib.util.module_from_spec(_spec)
sys.modules["migrate_dsn"] = migrate
_spec.loader.exec_module(migrate)

POOLER = "aws-0-ap-south-1.pooler.supabase.com"
TX_URI = f"postgresql://postgres.abc:s3cr3t@{POOLER}:6543/postgres?sslmode=require"


def test_transaction_pooler_moves_to_session_pooler() -> None:
    p = parse_dsn(migrate.session_dsn(TX_URI))
    assert p["port"] == "5432" and p["host"] == POOLER
    # everything else is kept: user, password, database, options
    assert p["user"] == "postgres.abc" and p["password"] == "s3cr3t"
    assert p["dbname"] == "postgres" and p["sslmode"] == "require"


def test_key_value_form_too() -> None:
    dsn = f"host={POOLER} port=6543 user=postgres.abc password=x dbname=postgres"
    assert parse_dsn(migrate.session_dsn(dsn))["port"] == "5432"


def test_everything_else_is_left_alone() -> None:
    for dsn in (
        f"postgresql://postgres.abc:x@{POOLER}:5432/postgres",  # already session
        "postgresql://postgres:x@db.abc.supabase.co:5432/postgres",  # direct
        "postgresql://postgres:ci-only@localhost:5432/postgres",
        "host=/tmp/pg port=55432 user=postgres dbname=postgres",
        "postgresql://u:p@db.example.com:6543/x",  # 6543 elsewhere is not Supabase's pooler
    ):
        assert migrate.session_dsn(dsn) == dsn


def test_describe_never_shows_credentials() -> None:
    text = migrate.describe_dsn(TX_URI)
    assert text == f"Supabase transaction pooler ({POOLER}:6543)"
    assert "s3cr3t" not in text and "postgres.abc" not in text
    assert migrate.describe_dsn(migrate.session_dsn(TX_URI)).startswith("Supabase session pooler")
    assert migrate.describe_dsn(
        "postgresql://postgres:x@db.abc.supabase.co:5432/postgres"
    ).startswith("Supabase direct connection")
