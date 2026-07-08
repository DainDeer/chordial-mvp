"""migration test for a68de0c288b5: conversation_history -> conversation_events.

builds a real sqlite db at the PREVIOUS alembic revision, seeds old-style
rows, runs `upgrade head`, and asserts the copy mapping (role -> author_type/
author, everything kind='message'), the original ordering, and that the old
table is gone while compressed_messages survives without its dead fk.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

_PREV_REVISION = "9f0b92c27ce6"   # agenda_snapshots (the revision before events)


def _alembic_config(db_url: str):
    from alembic.config import Config as AlembicConfig
    cfg = AlembicConfig(os.path.join(_PROJECT_ROOT, "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    # env.py reads Config.DATABASE_URL; override via the attribute it honors
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture()
def migrated_db(monkeypatch):
    """a temp db taken to the pre-events revision and seeded with old rows,
    returned alongside a function that upgrades it to head."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"

    # alembic/env.py resolves the url from app Config at import time; point it
    # at the temp db for the duration
    monkeypatch.setenv("DATABASE_URL", url)
    from config import Config as AppConfig
    monkeypatch.setattr(AppConfig, "DATABASE_URL", url)

    from alembic import command
    cfg = _alembic_config(url)
    command.upgrade(cfg, _PREV_REVISION)

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (uuid, preferred_name) VALUES ('u1', 'dain')"))
        conn.execute(text("""
            INSERT INTO conversation_history (user_uuid, platform, role, content, message_type, created_at) VALUES
              ('u1','discord','user','hello','conversation','2026-07-01 10:00:00'),
              ('u1','discord','assistant','hi there!','conversation','2026-07-01 10:00:05'),
              ('u1','discord','assistant','checking in~','scheduled','2026-07-01 12:00:00')
        """))
        conn.execute(text("""
            INSERT INTO compressed_messages
              (conversation_history_id, user_uuid, platform, role, original_length,
               compressed_content, compressed_length, compression_ratio)
            VALUES (1, 'u1', 'discord', 'user', 5, 'hi', 2, 0.4)
        """))

    def upgrade_to_head():
        command.upgrade(cfg, "head")

    yield engine, upgrade_to_head
    engine.dispose()
    os.unlink(path)


def test_upgrade_copies_history_into_events(migrated_db):
    engine, upgrade = migrated_db
    upgrade()

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT author_type, author, kind, content, message_type "
            "FROM conversation_events ORDER BY id"
        )).fetchall()

    assert [tuple(r) for r in rows] == [
        ("user", "user", "message", "hello", "conversation"),
        ("agent", "chordial", "message", "hi there!", "conversation"),
        ("agent", "chordial", "message", "checking in~", "scheduled"),
    ]


def test_upgrade_drops_old_table_and_dead_fk(migrated_db):
    engine, upgrade = migrated_db
    upgrade()

    insp = inspect(engine)
    tables = insp.get_table_names()
    assert "conversation_history" not in tables
    assert "conversation_events" in tables
    assert "compressed_messages" in tables   # legacy table survives

    # the fk to the retired table is gone; the users fk remains
    fk_targets = {fk["referred_table"] for fk in insp.get_foreign_keys("compressed_messages")}
    assert "conversation_history" not in fk_targets

    with engine.connect() as conn:
        kept = conn.execute(text(
            "SELECT conversation_history_id, compressed_content FROM compressed_messages"
        )).fetchone()
    assert tuple(kept) == (1, "hi")   # legacy pointer kept as plain integer
