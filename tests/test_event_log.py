"""event log tests: ordering, message-window semantics, and the scheduler-facing
last_message view.

the invariants that matter:
- id order is THE order (single writer + autoincrement), regardless of
  created_at ties
- recent(N) counts only kind='message' toward the window; action events ride
  along inside it instead of eating it
- last_message() never returns an action event, so a trailing tool action can't
  masquerade as "the assistant just replied" to the scheduler
"""
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, ConversationEvent  # noqa: E402
from src.managers.event_log import EventLog, cleanup_old_events  # noqa: E402


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain"))
        s.commit()
    yield TestSession
    engine.dispose()


def _log():
    return EventLog("u1", "discord")


def _seed_action(log, content="create_task {...} -> created"):
    """action events land in PR B via append_action; until then, write the row
    directly to exercise the read paths' kind-filtering."""
    return log._append(
        author_type="agent", author="chordial", kind="action",
        content=content, message_type=None, metadata={"tool": "create_task"},
    )


def test_append_and_recent_round_trip(db):
    log = _log()
    log.append_message("user", "user", "hello")
    log.append_message("agent", "chordial", "hi!")

    events = log.recent()
    assert [e.content for e in events] == ["hello", "hi!"]
    assert [e.role for e in events] == ["user", "assistant"]
    assert [e.author for e in events] == ["user", "chordial"]
    assert all(e.db_id is not None for e in events)


def test_recent_orders_by_id_not_created_at(db):
    log = _log()
    e1 = log.append_message("user", "user", "first")
    e2 = log.append_message("agent", "chordial", "second")
    # force a created_at tie/inversion; id order must still win
    with db() as s:
        row = s.query(ConversationEvent).filter_by(id=e2.db_id).one()
        row.created_at = s.query(ConversationEvent).filter_by(id=e1.db_id).one().created_at
        s.commit()
    assert [e.content for e in log.recent()] == ["first", "second"]


def test_recent_window_counts_only_messages(db):
    log = _log()
    log.append_message("user", "user", "old message")       # outside window
    log.append_message("agent", "chordial", "m1")
    _seed_action(log)                                        # rides along
    log.append_message("user", "user", "m2")

    events = log.recent(message_limit=2)
    # window = last 2 messages (m1, m2) + the action between them; "old" is out
    assert [e.content for e in events] == ["m1", "create_task {...} -> created", "m2"]
    assert [e.kind for e in events] == ["message", "action", "message"]


def test_last_message_skips_action_events(db):
    log = _log()
    log.append_message("agent", "chordial", "did the thing!", message_type="scheduled")
    _seed_action(log)  # trailing action AFTER the reply

    last = log.last_message()
    assert last.kind == "message"
    assert last.content == "did the thing!"
    assert last.message_type == "scheduled"
    assert last.role == "assistant"


def test_last_message_none_when_empty(db):
    assert _log().last_message() is None
    assert _log().recent() == []


def test_channels_are_isolated(db):
    discord = EventLog("u1", "discord")
    web = EventLog("u1", "web")
    discord.append_message("user", "user", "on discord")
    assert web.recent() == []
    assert web.last_message() is None


def test_clear_wipes_only_this_channel(db):
    discord = EventLog("u1", "discord")
    web = EventLog("u1", "web")
    discord.append_message("user", "user", "a")
    web.append_message("user", "user", "b")
    discord.clear()
    assert discord.recent() == []
    assert [e.content for e in web.recent()] == ["b"]


def test_cleanup_trims_to_most_recent(db):
    log = _log()
    for i in range(10):
        log.append_message("user", "user", f"m{i}")
    cleanup_old_events(max_per_user=4)
    events = log.recent(message_limit=100)
    assert [e.content for e in events] == ["m6", "m7", "m8", "m9"]
