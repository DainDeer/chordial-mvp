"""ChatService's introduction routing (docs/V3_DESIGN.md section 3).

v1's rigid onboarding state machine is retired; "has this user finished
onboarding" is no longer chat_service state at all - it's chordial's own
HelperState.status, read the same way any other helper's relationship state
is read. these tests cover the routing decision ChatService.process_message
makes (introduction vs. ordinary user_message stimulus) and the
begin_introduction deep-link entry point, with the orchestrator mocked out
(what happens once an introduction stimulus reaches a real Orchestrator/
HelperAgent is covered in tests/test_introduction.py).
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
from src.database.models import Base  # noqa: E402
from src.managers.helper_state_manager import HelperStateManager  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.chat_service import ChatService  # noqa: E402
from src.services.orchestration_types import Deliverable  # noqa: E402
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


class RecordingOrchestrator:
    """stands in for the real Orchestrator: records every Stimulus it was
    handed and returns a canned Deliverable."""

    def __init__(self, deliverable=None):
        self.calls = []
        self._deliverable = deliverable or Deliverable(text="hi there")

    async def handle(self, stimulus):
        self.calls.append(stimulus)
        return self._deliverable


def _msg(content: str, **kwargs) -> UnifiedMessage:
    return UnifiedMessage(
        content=content, platform_user_id=kwargs.pop("platform_user_id", "123"),
        platform=kwargs.pop("platform", "discord"),
        platform_message_id="m1", metadata={"username": "tester"},
        **kwargs,
    )


def _chat_service(deliverable=None):
    user_manager = UserManager()
    orchestrator = RecordingOrchestrator(deliverable)
    return ChatService(orchestrator=orchestrator, user_manager=user_manager), orchestrator, user_manager


# --- routing decision --------------------------------------------------------

def test_new_user_gets_introduction_stimulus(db):
    chat, orchestrator, _ = _chat_service()
    reply = run(chat.process_message(_msg("hello chordial")))

    assert reply == "hi there"
    assert len(orchestrator.calls) == 1
    stim = orchestrator.calls[0]
    assert stim.kind == "introduction"
    assert stim.chat_scope == "dm"
    assert stim.dm_helper == "chordial"
    assert stim.intro_helper == "chordial"


def test_new_user_moves_chordial_state_to_introducing(db):
    chat, _, _ = _chat_service()
    run(chat.process_message(_msg("hello chordial")))

    with db() as s:
        from src.database.models import User
        user_uuid = s.query(User).first().uuid

    state = run(HelperStateManager().get(user_uuid, "chordial"))
    assert state.status == "introducing"


def test_legacy_user_without_preferred_name_still_treated_as_introducing(db):
    """a pre-v3 user: has a platform identity (not brand new) but never
    finished the old name step, and has no HelperState row at all. back-compat
    signal: still routes to introduction."""
    chat, orchestrator, user_manager = _chat_service()
    # simulate a pre-existing (un-onboarded) identity, no HelperState row
    user_uuid, _ = run(user_manager.get_or_create_user("discord", "999", "legacy"))

    reply = run(chat.process_message(_msg("hi", platform_user_id="999")))

    assert reply == "hi there"
    assert orchestrator.calls[0].kind == "introduction"


def test_returning_active_user_gets_user_message_stimulus(db):
    chat, orchestrator, user_manager = _chat_service()
    user_uuid, _ = run(user_manager.get_or_create_user("discord", "456", "dain"))
    run(user_manager.update_user_preferences(user_uuid, {"preferred_name": "Dain"}))
    run(HelperStateManager().set_status(user_uuid, "chordial", "active"))

    reply = run(chat.process_message(_msg("hey again", platform_user_id="456")))

    assert reply == "hi there"
    assert len(orchestrator.calls) == 1
    stim = orchestrator.calls[0]
    assert stim.kind == "user_message"
    assert stim.intro_helper is None


def test_active_user_with_no_saved_name_is_not_re_introduced(db):
    """regression: an active chordial whose preferred_name never got persisted
    must NOT loop back into onboarding. keyed on status, not the name."""
    chat, orchestrator, user_manager = _chat_service()
    user_uuid, _ = run(user_manager.get_or_create_user("discord", "789"))
    # deliberately DO NOT set preferred_name - reproduce the stuck state
    run(HelperStateManager().set_status(user_uuid, "chordial", "active"))

    run(chat.process_message(_msg("what's up", platform_user_id="789")))

    assert orchestrator.calls[0].kind == "user_message"


def test_still_introducing_rules():
    from src.services.chat_service import _still_introducing
    assert _still_introducing("active", None) is False      # the regression
    assert _still_introducing("active", "Dain") is False
    assert _still_introducing("introducing", "Dain") is True
    assert _still_introducing("not_met", None) is True      # brand-new user
    assert _still_introducing("not_met", "Dain") is False   # pre-v3, already named


def test_group_scope_returns_none(db):
    chat, orchestrator, _ = _chat_service(deliverable=Deliverable(handled=True))
    reply = run(chat.process_message(_msg(
        "@tempo how's my training plan",
        chat_scope="group", group_chat_id="-100", dm_helper="chordial",
    )))

    assert reply is None
    assert orchestrator.calls[0].chat_scope == "group"


def test_refused_and_errored_map_to_in_character_copy(db):
    from src.services.chat_service import REFUSAL_REPLY, ERROR_REPLY

    chat, _, user_manager = _chat_service(deliverable=Deliverable(refused=True))
    user_uuid, _ = run(user_manager.get_or_create_user("discord", "1", "a"))
    run(HelperStateManager().set_status(user_uuid, "chordial", "active"))
    run(user_manager.update_user_preferences(user_uuid, {"preferred_name": "a"}))
    reply = run(chat.process_message(_msg("do something bad", platform_user_id="1")))
    assert reply == REFUSAL_REPLY

    chat2, _, user_manager2 = _chat_service(deliverable=Deliverable(errored=True))
    user_uuid2, _ = run(user_manager2.get_or_create_user("discord", "2", "b"))
    run(HelperStateManager().set_status(user_uuid2, "chordial", "active"))
    run(user_manager2.update_user_preferences(user_uuid2, {"preferred_name": "b"}))
    reply2 = run(chat2.process_message(_msg("hi", platform_user_id="2")))
    assert reply2 == ERROR_REPLY


def test_echo_fallback_when_no_orchestrator(db):
    chat = ChatService(orchestrator=None, user_manager=UserManager())
    reply = run(chat.process_message(_msg("hello")))
    assert reply == "echo: hello"


# --- begin_introduction (the meet-the-guides deep link) ----------------------

def test_begin_introduction_sets_helper_introducing_and_returns_reply(db):
    chat, orchestrator, user_manager = _chat_service(deliverable=Deliverable(text="hey, i'm tempo"))
    user_uuid, _ = run(user_manager.get_or_create_user("telegram", "789", "dain"))

    reply = run(chat.begin_introduction("telegram", "789", "tempo"))

    assert reply == "hey, i'm tempo"
    assert len(orchestrator.calls) == 1
    stim = orchestrator.calls[0]
    assert stim.kind == "introduction"
    assert stim.chat_scope == "dm"
    assert stim.dm_helper == "tempo"
    assert stim.intro_helper == "tempo"

    state = run(HelperStateManager().get(user_uuid, "tempo"))
    assert state.status == "introducing"
