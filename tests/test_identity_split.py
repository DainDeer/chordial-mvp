"""the user/stream identity split (phase 2a, ROOMS_DESIGN.md section 11).

user_id answers "whose reality?"; stream_id answers "which conversation?".
these tests pin the seam from every direction: the accessors' fallback
semantics, tools acting for the metadata user (not the stream), stimuli
carrying the thread, and usage attribution resolving streams to users.
the load-bearing assertion style: stream_id is a ROOM-shaped stranger
("room-1") and everything must still land on the right user.
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dainframe.providers.types import Usage
from dainframe.loop.usage import ProviderCallUsage
from dainframe.tools.context import ToolContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, UsageLog, User
from src.services import identity
from src.services.usage_recorder import UsageRecorder
from src.web import device_auth

U1 = "user-one"


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
        s.commit()
    yield TestSession
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


# --- the accessors ------------------------------------------------------------


def test_accessors_prefer_the_explicit_user_id():
    stimulus = SimpleNamespace(stream_id="room-1", extras={"user_id": U1})
    briefing = SimpleNamespace(stream_id="room-1", extras={"user_id": U1})
    context = ToolContext(stream_id="room-1", activation_id="a",
                          actor="pip", metadata={"user_id": U1})
    assert identity.user_of_stimulus(stimulus) == U1
    assert identity.user_of_briefing(briefing) == U1
    assert identity.user_of_context(context) == U1


def test_accessors_fall_back_to_the_legacy_equality():
    """a legacy stream IS its user - unthreaded callers keep working."""
    stimulus = SimpleNamespace(stream_id=U1, extras={})
    briefing = SimpleNamespace(stream_id=U1, extras=None)
    context = ToolContext(stream_id=U1, activation_id="a", actor="pip")
    assert identity.user_of_stimulus(stimulus) == U1
    assert identity.user_of_briefing(briefing) == U1
    assert identity.user_of_context(context) == U1
    assert identity.resolve_stream_user(U1) == U1


# --- tools act for the metadata user, never the stream ------------------------


def test_tool_acts_for_the_user_not_the_stream(env):
    """a room-shaped stream id must not leak into tool effects: the device
    code minted in 'room-1' belongs to U1."""
    from src.services.tools.link_tools import LINK_DEVICE

    context = ToolContext(stream_id="room-1", activation_id="test-act",
                          actor="chordial",
                          metadata={"scope": "dm", "user_id": U1})
    result = _run(LINK_DEVICE.handler({}, context))
    code = result.split("device link code:")[1].split()[0]
    linked = device_auth.link_device(code, "test")
    assert linked is not None
    assert device_auth.verify_device_token(linked[1]).user_uuid == U1


# --- stimuli carry the thread -------------------------------------------------


def test_chat_service_stimulus_carries_user_id(env):
    from src.models.unified_message import UnifiedMessage
    from src.services.chat_service import ChatService

    captured = {}

    class CapturingOrchestrator:
        async def handle(self, stimulus):
            captured["stimulus"] = stimulus
            return SimpleNamespace(any_delivered=True, lines=())

    service = ChatService(orchestrator=CapturingOrchestrator())
    _run(service.process_message(UnifiedMessage(
        content="hello", platform_user_id="discord-123", platform="discord",
        platform_message_id="m1")))
    stimulus = captured["stimulus"]
    assert stimulus.extras["user_id"] == stimulus.stream_id  # legacy equality
    assert stimulus.extras["user_id"]  # and it is actually set


def test_pulse_stimuli_carry_user_id(env):
    from dainframe.pulse import FiringPlan, RhythmKey
    from dainframe.core import DeliveryTarget
    from src.services.pulse_wiring import ChordialStimulusFactory

    factory = ChordialStimulusFactory(user_manager=SimpleNamespace())
    plan = FiringPlan(
        key=RhythmKey(stream_id=U1, rhythm_id="checkin"),
        kind="scheduled_tick",
        due_at=None,
        actor="chordial",
        target=DeliveryTarget(platform="telegram", target_id="t-1"),
    )
    stimulus = _run(factory.build(plan))
    assert stimulus.extras["user_id"] == U1

    curation_plan = FiringPlan(
        key=RhythmKey(stream_id=U1, rhythm_id="curation"),
        kind="curation_due", due_at=None)
    stimulus = _run(factory.build(curation_plan))
    assert stimulus.extras["user_id"] == U1


# --- usage attribution --------------------------------------------------------


def test_usage_resolves_stream_to_user(env):
    recorder = UsageRecorder(
        stream_user_resolver=lambda stream: U1 if stream == "room-1" else stream)
    _run(recorder.emit(ProviderCallUsage(
        stream_id="room-1", provider="anthropic", model="test-model",
        turn_kind="conversation",
        usage=Usage(input_tokens=10, output_tokens=5,
                    cache_read_tokens=0, cache_write_tokens=0),
        actor="pip", platform="app")))
    with db_mod.SessionLocal() as s:
        row = s.query(UsageLog).one()
        assert row.user_uuid == U1          # not "room-1"
        assert row.helper_id == "pip"


def test_usage_default_resolver_keeps_legacy_equality(env):
    recorder = UsageRecorder()
    _run(recorder.emit(ProviderCallUsage(
        stream_id=U1, provider="anthropic", model="test-model",
        turn_kind="conversation",
        usage=Usage(input_tokens=1, output_tokens=1,
                    cache_read_tokens=0, cache_write_tokens=0),
        actor="chordial")))
    with db_mod.SessionLocal() as s:
        assert s.query(UsageLog).one().user_uuid == U1
