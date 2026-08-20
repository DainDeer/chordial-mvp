"""the schema must be born on postgres, not just live there (phase 7c).

both fresh-install paths - init_fresh_db's create_all and `alembic upgrade
head` - emit DDL, and sqlite quietly accepts constructs postgres refuses.
the drill (scripts/drill.py) caught two in its first postgres run: a boolean
column whose server_default rendered as the integer 0, and a migration
constraint name over postgres's 63-char identifier cap. these tests pin the
whole class by compiling the model schema under the postgres dialect -
no server needed, both failure modes are visible at compile time.

(migration files' inline names aren't covered by model compilation; parity
between models.py and the migration chain is the repo's standing convention,
and the chain itself was applied end-to-end on scratch postgres when these
landed.)
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
