"""global test setup.

Forces DATABASE_URL to a throwaway database *before* anything imports
config/database. Without this, whichever test module imports
`src.database.database` first binds the engine to the real chordial.db (the
default in config.py), and DB-backed tests write their fixtures - e.g.
`tester` users with a fake discord id - straight into production. conftest.py
is imported by pytest before any test module, so setting it here wins the race
globally.

Default is a temp sqlite file (fast, zero setup). Set TEST_DATABASE_URL to run
the same suite against another engine - the postgres lane from
NATIVE_MIGRATION_PLAN §2.7:

    createdb chordial_test   # once
    TEST_DATABASE_URL=postgresql+psycopg://dain@localhost/chordial_test \
        poetry run pytest

The override must contain "test" in its database name (same guard rationale as
above: a typo must never point the suite at a real database). Its schema is
dropped, rebuilt from the models, and stamped at alembic head each session -
create_all + stamp rather than letting init_db() replay the migration chain,
because the chain's history is sqlite-shaped (NATIVE_MIGRATION_PLAN §2.3) and
because tests that call init_db() expect it to be a no-op on a current schema.
"""
import os
import sys
import tempfile

_override = os.environ.get("TEST_DATABASE_URL")
if _override:
    if "test" not in _override.rsplit("/", 1)[-1]:
        raise RuntimeError(
            f"TEST_DATABASE_URL database name must contain 'test', got: {_override}")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sqlalchemy import create_engine, text
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from src.database.models import Base  # imports nothing that reads config

    _ini = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "alembic.ini")
    _head = ScriptDirectory.from_config(AlembicConfig(_ini)).get_current_head()
    _engine = create_engine(_override)
    with _engine.begin() as _conn:
        _conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.drop_all(_conn)
        Base.metadata.create_all(_conn)
        _conn.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
        _conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _head})
    _engine.dispose()
    os.environ["DATABASE_URL"] = _override
else:
    _fd, _path = tempfile.mkstemp(suffix="_test.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{_path}"
