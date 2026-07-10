"""the proactivity gate: pure event-log arithmetic that silences a helper (or
the whole crew) once too many scheduled messages have gone unanswered.

isolated temp-file sqlite db (same pattern as test_delivery_eligibility.py),
events inserted directly so each test controls exact timestamps/authors.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, ConversationEvent  # noqa: E402
from src.managers.event_log import EventLog  # noqa: E402
from src.services.proactivity_gate import ProactivityGate  # noqa: E402
from config import Config  # noqa: E402


def run(coro):
    return asyncio.run(coro)


NOW = datetime(2026, 7, 9, 12, 0, 0)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr("src.services.proactivity_gate.utc_now", lambda: NOW)


def _make_user(db) -> str:
    with db() as s:
        user = User(preferred_name="tester")
        s.add(user)
        s.commit()
        return user.uuid


def _add_event(db, user_uuid, *, author_type, author, kind, content,
                message_type=None, created_at=NOW):
    with db() as s:
        s.add(ConversationEvent(
            user_uuid=user_uuid, platform="discord",
            author_type=author_type, author=author, kind=kind,
            content=content, message_type=message_type, created_at=created_at,
        ))
        s.commit()


def _user_msg(db, user_uuid, content="hi", created_at=NOW):
    _add_event(db, user_uuid, author_type="user", author="user", kind="message",
               content=content, message_type="conversation", created_at=created_at)


def _scheduled_msg(db, user_uuid, author="chordial", content="checking in~", created_at=NOW):
    _add_event(db, user_uuid, author_type="agent", author=author, kind="message",
               content=content, message_type="scheduled", created_at=created_at)


def _conversation_reply(db, user_uuid, author="chordial", content="sure!", created_at=NOW):
    _add_event(db, user_uuid, author_type="agent", author=author, kind="message",
               content=content, message_type="conversation", created_at=created_at)


def _note(db, user_uuid, content="platform switch", created_at=NOW):
    _add_event(db, user_uuid, author_type="system", author="system", kind="note",
               content=content, created_at=created_at)


def _action(db, user_uuid, author="chordial", content="create_task {} -> ok", created_at=NOW):
    _add_event(db, user_uuid, author_type="agent", author=author, kind="action",
               content=content, created_at=created_at)


def _check(user_uuid, helper_id="chordial"):
    return ProactivityGate().check(EventLog(user_uuid), helper_id)


# --- fresh conversation ------------------------------------------------------

def test_empty_log_is_allowed(db):
    uuid = _make_user(db)
    decision = _check(uuid)
    assert decision.allowed is True


def test_last_message_from_user_is_allowed(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=1))
    decision = _check(uuid)
    assert decision.allowed is True


def test_last_message_a_conversation_reply_is_allowed(db):
    """an agent reply to the user (message_type='conversation') is not a
    proactive message - it doesn't count toward unanswered at all."""
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=2))
    _conversation_reply(db, uuid, created_at=NOW - timedelta(hours=1))
    decision = _check(uuid)
    assert decision.allowed is True


# --- exponential backoff -----------------------------------------------------

def test_one_unanswered_denied_inside_three_hours(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=10))
    _scheduled_msg(db, uuid, created_at=NOW - timedelta(hours=2))  # 2h ago, need 3h
    decision = _check(uuid)
    assert decision.allowed is False
    assert "backoff" in decision.reason


def test_one_unanswered_allowed_after_three_hours(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=10))
    _scheduled_msg(db, uuid, created_at=NOW - timedelta(hours=3, minutes=1))
    decision = _check(uuid)
    assert decision.allowed is True


def test_two_unanswered_require_six_hours(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=20))
    _scheduled_msg(db, uuid, created_at=NOW - timedelta(hours=10))
    _scheduled_msg(db, uuid, created_at=NOW - timedelta(hours=5))  # newest 5h ago, need 6h
    decision = _check(uuid)
    assert decision.allowed is False
    assert "backoff" in decision.reason

    # bump the newest unanswered back past 6h -> now allowed
    with db() as s:
        row = s.query(ConversationEvent).order_by(ConversationEvent.id.desc()).first()
        row.created_at = NOW - timedelta(hours=6, minutes=1)
        s.commit()
    decision = _check(uuid)
    assert decision.allowed is True


def test_three_unanswered_require_twelve_hours(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(days=2))
    # keep this under the per-helper cap by spreading across helpers
    _scheduled_msg(db, uuid, author="tempo", created_at=NOW - timedelta(hours=40))
    _scheduled_msg(db, uuid, author="tempo", created_at=NOW - timedelta(hours=20))
    _scheduled_msg(db, uuid, author="tempo", created_at=NOW - timedelta(hours=11))  # need 12h
    decision = _check(uuid, helper_id="chordial")
    assert decision.allowed is False
    assert "backoff" in decision.reason


# --- per-helper cap -----------------------------------------------------------

def test_per_helper_cap_denies_at_three_regardless_of_elapsed_time(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(days=5))
    for hours_ago in (100, 80, 60):
        _scheduled_msg(db, uuid, author="chordial", created_at=NOW - timedelta(hours=hours_ago))
    decision = _check(uuid, helper_id="chordial")
    assert decision.allowed is False
    assert "cap" in decision.reason


def test_per_helper_cap_does_not_silence_other_helpers(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(days=5))
    for hours_ago in (100, 80, 60):
        _scheduled_msg(db, uuid, author="chordial", created_at=NOW - timedelta(hours=hours_ago))
    # tempo hasn't sent anything - only 3 total unanswered so far (under crew cap of 4)
    decision = _check(uuid, helper_id="tempo")
    assert decision.allowed is True


# --- crew cap ------------------------------------------------------------------

def test_crew_cap_denies_at_four_mixed_authors(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(days=5))
    for hours_ago, author in ((100, "chordial"), (80, "tempo"), (60, "chordial"), (40, "tempo")):
        _scheduled_msg(db, uuid, author=author, created_at=NOW - timedelta(hours=hours_ago))
    decision = _check(uuid, helper_id="chordial")
    assert decision.allowed is False
    assert "crew cap" in decision.reason

    # crew cap silences everyone, not just helpers with their own messages in the chain
    decision = _check(uuid, helper_id="someone_else")
    assert decision.allowed is False
    assert "crew cap" in decision.reason


# --- user reply resets everything ----------------------------------------------

def test_user_reply_resets_the_chain(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(days=5))
    for hours_ago, author in ((100, "chordial"), (80, "tempo"), (60, "chordial"), (40, "tempo")):
        _scheduled_msg(db, uuid, author=author, created_at=NOW - timedelta(hours=hours_ago))
    assert _check(uuid).allowed is False  # crew cap, sanity check

    _user_msg(db, uuid, content="oh hey sorry", created_at=NOW - timedelta(minutes=5))
    decision = _check(uuid)
    assert decision.allowed is True
    assert decision.reason == "clear"


# --- notes/actions are invisible to the gate ------------------------------------

def test_notes_do_not_count_as_a_user_reply(db):
    """a system note (e.g. platform-switch notice) must not reset the chain -
    only a real user message does."""
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=10))
    _scheduled_msg(db, uuid, created_at=NOW - timedelta(hours=2))  # 2h ago, need 3h
    _note(db, uuid, created_at=NOW - timedelta(hours=1))
    decision = _check(uuid)
    assert decision.allowed is False
    assert "backoff" in decision.reason


def test_actions_do_not_count_as_unanswered_proactive_messages(db):
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=10))
    _scheduled_msg(db, uuid, created_at=NOW - timedelta(hours=3, minutes=1))
    _action(db, uuid, created_at=NOW - timedelta(hours=1))
    # still just 1 unanswered proactive message, and it cleared 3h ago
    decision = _check(uuid)
    assert decision.allowed is True


def test_actions_and_notes_are_invisible_to_the_user_anchor_too(db):
    """an action/note sandwiched between the user message and a scheduled
    message must not break the 'after the last user message' scan."""
    uuid = _make_user(db)
    _user_msg(db, uuid, created_at=NOW - timedelta(hours=10))
    _action(db, uuid, created_at=NOW - timedelta(hours=9))
    _note(db, uuid, created_at=NOW - timedelta(hours=8))
    _scheduled_msg(db, uuid, created_at=NOW - timedelta(hours=2))  # 2h ago, need 3h
    decision = _check(uuid)
    assert decision.allowed is False
    assert "backoff" in decision.reason


# --- sanity: config knobs are what the gate is built against -------------------

def test_config_knobs_match_spec_defaults():
    assert Config.GATE_PER_HELPER_CAP == 3
    assert Config.GATE_CREW_CAP == 4
    assert Config.GATE_BASE_INTERVAL_HOURS == 3.0
