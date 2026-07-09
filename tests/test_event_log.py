"""event log tests: ordering, message-window semantics, unified cross-platform
history, and the scheduler-facing views.

the invariants that matter:
- id order is THE order (single writer + autoincrement), regardless of
  created_at ties
- recent(N) counts only kind='message' toward the window; action events ride
  along inside it instead of eating it
- a user has ONE conversation across platforms: reads never filter platform,
  which lives on events as pure provenance
- last_message()/last_user_message() never return action or note events, so a
  trailing tool action or switch notice can't masquerade as a reply
- active_platform() = where the user last spoke
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
        s.add(User(uuid="u2", preferred_name="other"))
        s.commit()
    yield TestSession
    engine.dispose()


def _log(user="u1"):
    return EventLog(user)


def _seed_action(log, content="create_task {...} -> created", platform="discord"):
    """write an action row with exact content (bypassing the freezer) so
    assertions can match literal strings."""
    return log._append(
        author_type="agent", author="chordial", kind="action",
        content=content, message_type=None, metadata={"tool": "create_task"},
        platform=platform,
    )


def test_append_and_recent_round_trip(db):
    log = _log()
    log.append_message("user", "user", "hello", platform="discord")
    log.append_message("agent", "chordial", "hi!", platform="discord")

    events = log.recent()
    assert [e.content for e in events] == ["hello", "hi!"]
    assert [e.role for e in events] == ["user", "assistant"]
    assert [e.author for e in events] == ["user", "chordial"]
    assert [e.platform for e in events] == ["discord", "discord"]
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


def test_history_is_unified_across_platforms(db):
    """one conversation, however many doors: discord and telegram events
    interleave in a single stream, each tagged with where it happened."""
    log = _log()
    log.append_message("user", "user", "hi from discord", platform="discord")
    log.append_message("agent", "chordial", "hey!", platform="discord")
    log.append_message("user", "user", "now on telegram", platform="telegram")
    log.append_message("agent", "chordial", "same me!", platform="telegram")

    events = log.recent()
    assert [e.content for e in events] == [
        "hi from discord", "hey!", "now on telegram", "same me!",
    ]
    assert [e.platform for e in events] == ["discord", "discord", "telegram", "telegram"]


def test_users_are_isolated(db):
    _log("u1").append_message("user", "user", "mine")
    assert _log("u2").recent() == []
    assert _log("u2").last_message() is None


def test_last_message_skips_action_and_note_events(db):
    log = _log()
    log.append_message("agent", "chordial", "did the thing!", message_type="scheduled")
    _seed_action(log)  # trailing action AFTER the reply
    log.append_note("*(moved to telegram)*", platform="discord",
                    metadata={"note_type": "platform_switch"})

    last = log.last_message()
    assert last.kind == "message"
    assert last.content == "did the thing!"
    assert last.message_type == "scheduled"
    assert last.role == "assistant"


def test_last_user_message_and_active_platform(db):
    log = _log()
    assert log.last_user_message() is None
    assert log.active_platform() is None

    log.append_message("user", "user", "on discord", platform="discord")
    log.append_message("agent", "chordial", "hi!", platform="discord")
    assert log.active_platform() == "discord"

    log.append_message("user", "user", "now here", platform="telegram")
    # the agent's reply platform doesn't matter - user's last word does
    log.append_message("agent", "chordial", "hello again", platform="telegram")
    assert log.last_user_message().content == "now here"
    assert log.active_platform() == "telegram"


def test_append_note_shape(db):
    log = _log()
    note = log.append_note("*(psst)*", platform="discord",
                           metadata={"note_type": "platform_switch", "to": "telegram"})
    assert note.kind == "note"
    assert note.author_type == "system"
    assert note.author == "system"
    assert note.platform == "discord"
    assert note.message_type is None
    assert note.metadata["to"] == "telegram"


def test_last_message_none_when_empty(db):
    assert _log().last_message() is None
    assert _log().recent() == []


def test_clear_wipes_only_this_user(db):
    _log("u1").append_message("user", "user", "a")
    _log("u2").append_message("user", "user", "b")
    _log("u1").clear()
    assert _log("u1").recent() == []
    assert [e.content for e in _log("u2").recent()] == ["b"]


def test_cleanup_trims_to_most_recent(db):
    log = _log()
    for i in range(10):
        log.append_message("user", "user", f"m{i}")
    cleanup_old_events(max_per_user=4)
    events = log.recent(message_limit=100)
    assert [e.content for e in events] == ["m6", "m7", "m8", "m9"]
