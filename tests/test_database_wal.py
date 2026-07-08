"""regression test: the app's sqlite engine must run in WAL mode.

WAL is more resilient than the default rollback-journal mode to the kind of
connection hiccup that showed up as spurious "attempt to write a readonly
database" errors after a laptop slept overnight with the app still running -
rollback-journal mode has to create/delete a `-journal` file alongside the db
on every write, while WAL just appends to a separate log file.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database.database import engine, get_db  # noqa: E402


def test_sqlite_engine_uses_wal_mode():
    # exercise a real connection first (in case none has been opened yet)
    with get_db():
        pass

    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()

    assert mode.lower() == "wal"
    # NORMAL == 1 (sqlite's own recommended pairing for WAL)
    assert synchronous == 1
