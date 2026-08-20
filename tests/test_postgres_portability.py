"""the schema must be born on postgres, not just live there (phase 7c).

both fresh-install paths - init_fresh_db's create_all and `alembic upgrade
head` - emit DDL, and sqlite quietly accepts constructs postgres refuses.
the drill (scripts/drill.py) caught two in its first postgres run: a boolean
column whose server_default rendered as the integer 0, and a migration
constraint name over postgres's 63-char identifier cap. these tests pin the
whole class by compiling the model schema under the postgres dialect -
no server needed, both failure modes are visible at compile time.

the migration chain gets the same treatment (sol's #79 round): alembic's
offline mode renders every migration's SQL through the postgres dialect
without a server - identifier-length violations raise during rendering,
and the rendered DDL is scanned for non-boolean boolean defaults.
"""
import io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import Boolean
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from src.database.models import Base

_PG = postgresql.dialect()


def _pg_ddl(table) -> str:
    return str(CreateTable(table).compile(dialect=_PG))


def test_every_table_and_index_compiles_under_postgres():
    """compilation raises IdentifierError for any name past the 63-char
    cap - the exact failure that stopped `alembic upgrade head` cold."""
    for table in Base.metadata.sorted_tables:
        _pg_ddl(table)
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=_PG))


def test_boolean_server_defaults_render_as_booleans():
    """postgres refuses `BOOLEAN DEFAULT 0` (a text('0') smuggled onto a
    boolean column) - every boolean server_default must render as a real
    boolean literal."""
    checked = 0
    for table in Base.metadata.sorted_tables:
        ddl = _pg_ddl(table)
        for column in table.columns:
            if not isinstance(column.type, Boolean) or \
                    column.server_default is None:
                continue
            line = next(l for l in ddl.splitlines()
                        if re.match(rf'\s*"?{column.name}"? ', l))
            assert re.search(r"DEFAULT (false|true)", line), (
                f"{table.name}.{column.name}: boolean server_default "
                f"renders as non-boolean DDL: {line.strip()!r}")
            checked += 1
    assert checked >= 1  # device_events.rejected at minimum


def test_migration_chain_renders_under_postgres(monkeypatch):
    """the whole alembic chain, base to head, rendered through the postgres
    dialect in offline mode - no server, no connection. this is exactly
    where both drill findings would have surfaced: the 68-char constraint
    name raises IdentifierError during rendering, and the boolean-default
    scan below catches text('0') smuggled onto a boolean column in a
    migration (the model tests can't see migration files)."""
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from config import Config

    # env.py reads Config.DATABASE_URL at exec time; offline mode never
    # connects, so a fake postgres url is safe anywhere the suite runs
    monkeypatch.setattr(Config, "DATABASE_URL",
                        "postgresql+psycopg://nobody@nowhere/never_connects")
    buf = io.StringIO()
    cfg = AlembicConfig(str(REPO / "alembic.ini"), output_buffer=buf,
                        stdout=io.StringIO())
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    command.upgrade(cfg, "head", sql=True)
    sql = buf.getvalue()
    assert "CREATE TABLE" in sql  # the render actually happened
    for line in sql.splitlines():
        if "BOOLEAN" in line.upper():
            assert not re.search(r"DEFAULT\s+'?0'?", line, re.IGNORECASE), (
                f"boolean column with a non-boolean default in migration "
                f"DDL: {line.strip()!r}")
