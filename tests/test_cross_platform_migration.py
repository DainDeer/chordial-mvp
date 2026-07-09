"""migration test for ecce34c113d1: cross platform conversations.

builds a real sqlite db at the PREVIOUS alembic revision, seeds duplicate
platform identities (possible before the constraint existed) and conversation
events, runs `upgrade head`, and asserts: dupes deduped keeping the oldest,
unique constraint present, event index swapped to (user_uuid, id), link_codes
table created, and event rows untouched.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

_PREV_REVISION = "a68de0c288b5"   # conversation_events (the revision before this one)


def _alembic_config(db_url: str):
    from alembic.config import Config as AlembicConfig
    cfg = AlembicConfig(os.path.join(_PROJECT_ROOT, "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture()
def migrated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"

    monkeypatch.setenv("DATABASE_URL", url)
    from config import Config as AppConfig
    monkeypatch.setattr(AppConfig, "DATABASE_URL", url)

    from alembic import command
    cfg = _alembic_config(url)
    command.upgrade(cfg, _PREV_REVISION)

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (uuid, preferred_name) VALUES ('u1', 'dain')"))
        # duplicate identity rows - only possible pre-constraint
        conn.execute(text("""
            INSERT INTO platform_identities (user_uuid, platform, platform_user_id, is_active) VALUES
              ('u1', 'discord', '123', 1),
              ('u1', 'discord', '123', 1),
              ('u1', 'discord', '123', 0)
        """))
        conn.execute(text("""
            INSERT INTO conversation_events
              (user_uuid, platform, author_type, author, kind, content, message_type, event_metadata, created_at)
            VALUES
              ('u1','discord','user','user','message','hello','conversation','{}','2026-07-01 10:00:00'),
              ('u1','discord','agent','chordial','message','hi!','conversation','{}','2026-07-01 10:00:05')
        """))

    def upgrade_to_head():
        command.upgrade(cfg, "head")

    yield engine, upgrade_to_head
    engine.dispose()
    os.unlink(path)


def test_upgrade_dedupes_identities_and_adds_constraint(migrated_db):
    engine, upgrade = migrated_db
    upgrade()

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, is_active FROM platform_identities ORDER BY id"
        )).fetchall()
    # only the OLDEST row survived (id=1, the active one)
    assert len(rows) == 1
    assert rows[0][0] == 1

    insp = inspect(engine)
    uniques = {u["name"] for u in insp.get_unique_constraints("platform_identities")}
    assert "uq_platform_identity_platform_user" in uniques


def test_upgrade_swaps_event_index_and_keeps_rows(migrated_db):
    engine, upgrade = migrated_db
    upgrade()

    insp = inspect(engine)
    index_names = {ix["name"] for ix in insp.get_indexes("conversation_events")}
    assert "ix_conversation_events_user_id" in index_names
    assert "ix_conversation_events_user_platform_id" not in index_names

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT platform, author, content FROM conversation_events ORDER BY id"
        )).fetchall()
    assert [tuple(r) for r in rows] == [
        ("discord", "user", "hello"),
        ("discord", "chordial", "hi!"),
    ]


def test_upgrade_creates_link_codes(migrated_db):
    engine, upgrade = migrated_db
    upgrade()

    insp = inspect(engine)
    assert "link_codes" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("link_codes")}
    assert {"code", "user_uuid", "created_at", "expires_at", "used_at"} <= cols


def test_downgrade_round_trips(migrated_db):
    engine, upgrade = migrated_db
    upgrade()

    from alembic import command
    url = str(engine.url)
    cfg = _alembic_config(url)
    command.downgrade(cfg, _PREV_REVISION)

    insp = inspect(engine)
    assert "link_codes" not in insp.get_table_names()
    index_names = {ix["name"] for ix in insp.get_indexes("conversation_events")}
    assert "ix_conversation_events_user_platform_id" in index_names
