import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# make the project root importable so we can pull in the app's models + config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from src.database.models import Base  # noqa: E402  (imports every model -> registers on Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# the app is the single source of truth for the connection string; override
# whatever placeholder is in alembic.ini with the real runtime URL.
config.set_main_option("sqlalchemy.url", Config.DATABASE_URL)

# Interpret the config file for Python logging. Skipped when embedded (init_db
# sets configure_logger=False) so alembic doesn't clobber the app's own logging
# config; still applied for standalone `alembic ...` CLI runs.
if config.attributes.get("configure_logger", True) and config.config_file_name is not None:
    fileConfig(config.config_file_name)

# model metadata for 'autogenerate' support
target_metadata = Base.metadata

# sqlite can't ALTER/DROP columns in place; batch mode rebuilds-and-copies so
# future migrations that alter or drop columns work. harmless for plain creates.
_IS_SQLITE = Config.DATABASE_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_IS_SQLITE,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect and apply)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_IS_SQLITE,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
