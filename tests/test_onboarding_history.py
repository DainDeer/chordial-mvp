"""regression test: onboarding must persist as real conversation history.

before this fix, ChatService.process_message returned onboarding replies (the
welcome message, the name/timezone/memory prompts) without ever persisting
them. the event log stayed empty through onboarding, so the scheduler's "no
messages yet -> send immediately" rule couldn't tell a brand-new user apart
from someone who'd just finished onboarding - it fired a scheduled check-in
within minutes of onboarding completing, which felt jarring (worse once the
notion agenda digest made that first unprompted message sound uncannily
well-informed).

isolated temp db, no network/ai calls needed - onboarding's early return in
process_message never reaches the agent_service.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, ConversationEvent  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.chat_service import ChatService  # noqa: E402
from src.services.scheduler_service import SchedulerService  # noqa: E402
from src.models.unified_message import UnifiedMessage  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


def _msg(content: str) -> UnifiedMessage:
    return UnifiedMessage(
        content=content, platform_user_id="123", platform="discord",
        platform_message_id="m1", metadata={"username": "tester"},
    )


def _chat_service():
    user_manager = UserManager()
    return ChatService(
        orchestrator=None,  # onboarding never reaches the orchestrator
        user_manager=user_manager,
    ), user_manager


def _drive_onboarding(chat):
    """runs the full name -> timezone -> memory flow, returns the 4 replies."""
    r1 = run(chat.process_message(_msg("hello chordial")))       # welcome
    r2 = run(chat.process_message(_msg("Dain")))                 # name
    r3 = run(chat.process_message(_msg("california")))           # timezone
    r4 = run(chat.process_message(_msg("i like tea")))            # memory -> completes
    return r1, r2, r3, r4


def test_onboarding_exchange_is_persisted(db):
    chat, _ = _chat_service()
    replies = _drive_onboarding(chat)
    assert all(replies)  # every step returned a reply

    with db() as s:
        rows = s.query(ConversationEvent).order_by(ConversationEvent.id).all()

    # 4 user turns + 4 assistant turns, all message events, author-attributed
    assert [r.author_type for r in rows] == ["user", "agent"] * 4
    assert [r.author for r in rows] == ["user", "chordial"] * 4
    assert all(r.kind == "message" for r in rows)
    assert rows[0].content == "hello chordial"
    assert rows[-2].content == "i like tea"
    assert all(r.message_type == "conversation" for r in rows)


def test_scheduler_does_not_fire_immediately_after_onboarding(db):
    """the actual regression: previously should_send_scheduled_message saw
    zero history rows right after onboarding and returned True (send now)."""
    chat, user_manager = _chat_service()
    _drive_onboarding(chat)

    scheduler = SchedulerService(user_manager=user_manager)
    with db() as s:
        user_uuid = s.query(ConversationEvent).first().user_uuid

    should_send = run(scheduler.should_send_scheduled_message(user_uuid))
    assert should_send is False


def test_partial_onboarding_still_persists_each_turn(db):
    """even a mid-flow turn (e.g. just the name) should land in history, not
    only the final completing turn."""
    chat, _ = _chat_service()
    run(chat.process_message(_msg("hi")))
    run(chat.process_message(_msg("Dain")))

    with db() as s:
        rows = s.query(ConversationEvent).all()
    assert len(rows) == 4  # 2 user + 2 assistant turns so far
