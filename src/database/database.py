from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
import logging
import os

from config import Config

logger = logging.getLogger(__name__)

_IS_SQLITE = "sqlite" in Config.DATABASE_URL

# create engine
engine = create_engine(
    Config.DATABASE_URL,
    echo=False,  # set to true for sql query logging
    connect_args={"check_same_thread": False} if _IS_SQLITE else {},
    # the app runs for days at a time; postgres connections idling in the
    # pool get dropped by the server/OS eventually, and without pre-ping the
    # first query on a dead connection surfaces as a mid-request
    # OperationalError instead of a transparent reconnect
    pool_pre_ping=not _IS_SQLITE,
)

if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """WAL is far more resilient than the default rollback-journal mode to
        the kinds of connection hiccups that showed up as spurious "attempt to
        write a readonly database" errors after a laptop slept overnight with
        the app still running: rollback-journal mode has to create and delete
        a `-journal` file alongside the db on every single write, while WAL
        just appends to a separate log file. journal_mode is persisted in the
        db file itself, so this "upgrades" any existing db the first time a
        connection is made - no migration needed. synchronous=NORMAL is
        sqlite's own recommended pairing for WAL (still crash-safe; only an
        OS-level power-loss mid-write could lose the last commit, an
        acceptable tradeoff for a local file)."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

# create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# project root (…/chordial-mvp), for locating alembic.ini
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def init_db():
    """bring the database schema up to date by running alembic migrations.

    alembic is the single schema authority: the models are the source of truth,
    and migrations (generated from them) are how the schema is applied to any
    database - fresh or existing. `upgrade head` is idempotent, so this is a
    no-op once a db is current. runs automatically at startup so a new install
    or a `git pull` on the server needs no manual migration step.
    """
    from alembic.config import Config as AlembicConfig
    from alembic import command

    alembic_cfg = AlembicConfig(os.path.join(_PROJECT_ROOT, "alembic.ini"))
    # don't let alembic reconfigure the app's logging (see alembic/env.py)
    alembic_cfg.attributes["configure_logger"] = False

    logger.info("running database migrations (alembic upgrade head)...")
    command.upgrade(alembic_cfg, "head")
    logger.info("database schema is up to date")

@contextmanager
def get_db() -> Session:
    """provide a transactional scope for database operations"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# convenience function for async context
async def get_db_session() -> Session:
    """get a database session for async operations"""
    return SessionLocal()