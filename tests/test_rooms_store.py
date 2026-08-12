"""the room store: lifecycle, lazy close, the digest, and grandfathering
(phase 2b, ROOMS_DESIGN.md section 4).

the invariants under test: one daily room per (user, local day); the next
day's first interaction closes the previous room idempotently; exactly one
summary per room ever; the pre-rooms stream becomes a closed legacy room
whose uuid IS the user uuid; and stream->user resolution goes through the
rooms table with the legacy fallback agreeing on both paths.
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, ConversationEvent, Room, RoomSummary, User
from src.managers.event_log import EventLog
from src.services.identity import resolve_stream_user
from src.services.rooms import RoomStore

U1 = "user-one"
U2 = "user-two"
MON = date(2026, 8, 10)
TUE = date(2026, 8, 11)
WED = date(2026, 8, 12)


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="megan", timezone="UTC"))
        s.add(User(uuid=U2, preferred_name="guest", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


@pytest.fixture()
def store(env):
    return RoomStore()


def _room_message(room_uuid, user, author_type, author, content):
    import asyncio
    from dainframe.core.events import NewEvent
    from src.managers.event_store_adapter import SqlEventStore
    asyncio.run(SqlEventStore(room_uuid, user_uuid=user).append(NewEvent(
        author_type=author_type, author=author, kind="message",
        content=content, platform="app")))


# --- lazy create --------------------------------------------------------------


def test_current_room_is_lazy_and_stable(env, store):
    room = store.current_room(U1, TUE)
    assert room["room_type"] == "daily"
    assert room["status"] == "open"
    assert room["date"] == TUE.isoformat()
    again = store.current_room(U1, TUE)
    assert again["room_uuid"] == room["room_uuid"]   # same day, same room
    with env() as s:
        assert s.query(Room).filter(Room.room_type == "daily").count() == 1


def test_rooms_are_per_user(env, store):
    r1 = store.current_room(U1, TUE)
    r2 = store.current_room(U2, TUE)
    assert r1["room_uuid"] != r2["room_uuid"]


# --- the lazy close -----------------------------------------------------------


def test_next_day_closes_yesterday_with_a_digest(env, store):
    monday = store.current_room(U1, MON)
    _room_message(monday["room_uuid"], U1, "user", "user",
                  "i did the room model today!")
    _room_message(monday["room_uuid"], U1, "agent", "pip", "one whole block!!")

    tuesday = store.current_room(U1, TUE)
    assert tuesday["room_uuid"] != monday["room_uuid"]

    closed = store.get_by_uuid(monday["room_uuid"])
    assert closed["status"] == "closed"
    summary = store.latest_summary(U1)
    assert summary["date"] == MON.isoformat()
    assert "1 user messages, 1 helper replies" in summary["content"]
    assert "i did the room model today!" in summary["content"]
    assert "pip:" in summary["content"]


def test_close_is_idempotent_and_summary_unique(env, store):
    monday = store.current_room(U1, MON)
    room_id = store.get_by_uuid(monday["room_uuid"])["id"]
    store.close_room(room_id)
    store.close_room(room_id)                    # re-close: no-op
    store.current_room(U1, WED)                  # stale sweep sees it closed
    with env() as s:
        assert s.query(RoomSummary).filter(
            RoomSummary.room_id == room_id).count() == 1


def test_a_skipped_day_closes_all_stale_rooms(env, store):
    store.current_room(U1, MON)
    store.current_room(U1, WED)                  # tuesday never happened
    with env() as s:
        stale = s.query(Room).filter(
            Room.room_type == "daily", Room.date < WED).all()
        assert all(r.status == "closed" for r in stale)


# --- grandfathering -----------------------------------------------------------


def test_legacy_stream_becomes_a_closed_legacy_room(env, store):
    EventLog(U1).append_message("user", "user", "hello from the before times")
    room = store.current_room(U1, TUE)           # first touch of the era
    with env() as s:
        legacy = s.query(Room).filter(Room.room_type == "legacy").one()
        assert legacy.room_uuid == U1            # the stream ids line up
        assert legacy.status == "closed"
        assert s.query(RoomSummary).filter(
            RoomSummary.room_id == legacy.id).count() == 1
    # and only once
    store.current_room(U1, TUE)
    with env() as s:
        assert s.query(Room).filter(Room.room_type == "legacy").count() == 1
    assert room["room_type"] == "daily"


def test_no_history_means_no_legacy_room(env, store):
    store.current_room(U1, TUE)
    with env() as s:
        assert s.query(Room).filter(Room.room_type == "legacy").count() == 0


# --- resolution ---------------------------------------------------------------


def test_resolve_stream_user_via_rooms_and_fallback(env, store):
    room = store.current_room(U1, TUE)
    assert resolve_stream_user(room["room_uuid"]) == U1
    # legacy/unknown streams fall back to the equality
    assert resolve_stream_user(U2) == U2


def test_room_events_carry_both_keys(env, store):
    room = store.current_room(U1, TUE)
    _room_message(room["room_uuid"], U1, "user", "user", "hi")
    with env() as s:
        row = s.query(ConversationEvent).one()
        assert row.stream_id == room["room_uuid"]   # which conversation
        assert row.user_uuid == U1                  # whose reality
