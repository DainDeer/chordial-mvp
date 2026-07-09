"""orchestrator tests: selection, briefing, recording, and the tool-registry
view seam.

the orchestrator is deliberately deterministic in v2 - these lock down the
static selection map, the record order (user -> actions -> reply), the
"non-answers record nothing" invariant, and that scheduled replies carry
message_type='scheduled'. fake agents, isolated temp db.
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
from src.database.models import Base, User, ConversationEvent  # noqa: E402
from src.agents.base import AgentOutcome, Briefing  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.agent_service import ExecutedAction  # noqa: E402
from src.services.orchestrator import Orchestrator, Stimulus  # noqa: E402
from src.services.tools.base import Tool, ToolRegistry  # noqa: E402
from src.providers.ai.types import ToolDef, ProviderError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain", timezone="US/Pacific"))
        s.commit()
    yield TestSession
    engine.dispose()


class RecordingAgent:
    """a fake agent that captures its briefing and returns a canned outcome."""
    def __init__(self, name="chordial", outcome=None):
        self.name = name
        self.outcome = outcome or AgentOutcome(text="hi!")
        self.briefings: list[Briefing] = []

    async def act(self, briefing):
        self.briefings.append(briefing)
        return self.outcome


class FakeCurator:
    name = "curator"

    def __init__(self):
        self.curated: list[str] = []

    async def find_users_needing_curation(self):
        return ["u1"]

    async def act(self, briefing):
        self.curated.append(briefing.user_uuid)
        return AgentOutcome(text=None)


def _orch(agents, **kwargs):
    return Orchestrator(agents=agents, user_manager=UserManager(), **kwargs)


def _events(db):
    with db() as s:
        return [(e.kind, e.author_type, e.author, e.message_type) for e in
                s.query(ConversationEvent).order_by(ConversationEvent.id).all()]


# --- selection ---------------------------------------------------------------

def test_user_message_goes_to_companion(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    d = run(orch.handle(Stimulus(kind="user_message", user_uuid="u1",
                                 platform="discord", content="hello",
                                 user_name="dain", user_timezone="UTC")))
    assert d.text == "hi!"
    assert len(companion.briefings) == 1
    assert companion.briefings[0].kind == "user_message"


def test_scheduled_tick_goes_to_companion_with_checkin_briefing(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    run(orch.handle(Stimulus(kind="scheduled_tick", user_uuid="u1", platform="discord")))
    assert companion.briefings[0].kind == "scheduled_checkin"


def test_curation_due_goes_to_curator(db):
    curator = FakeCurator()
    orch = _orch({"chordial": RecordingAgent(), "curator": curator})
    run(orch.handle(Stimulus(kind="curation_due", user_uuid="u1")))
    assert curator.curated == ["u1"]
    assert run(orch.curation_candidates()) == ["u1"]


def test_unknown_stimulus_selects_nobody(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    d = run(orch.handle(Stimulus(kind="mystery", user_uuid="u1")))
    assert d.text is None
    assert companion.briefings == []


# --- briefing assembly ---------------------------------------------------------

def test_briefing_includes_inbound_message_as_last_event(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    run(orch.handle(Stimulus(kind="user_message", user_uuid="u1", platform="discord",
                             content="the new message", user_name="dain", user_timezone="UTC")))
    events = companion.briefings[0].events
    assert events[-1].content == "the new message"
    assert events[-1].role == "user"


def test_scheduler_stimulus_resolves_profile_from_db(db):
    """the scheduler doesn't resolve names/timezones; the orchestrator must."""
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    run(orch.handle(Stimulus(kind="scheduled_tick", user_uuid="u1", platform="discord")))
    b = companion.briefings[0]
    assert b.user_name == "dain"
    assert b.user_timezone == "US/Pacific"


# --- recording -----------------------------------------------------------------

def test_scheduled_reply_gets_scheduled_message_type(db):
    companion = RecordingAgent(outcome=AgentOutcome(text="checking in~"))
    orch = _orch({"chordial": companion})
    run(orch.handle(Stimulus(kind="scheduled_tick", user_uuid="u1", platform="discord")))
    assert _events(db) == [("message", "agent", "chordial", "scheduled")]


def test_record_order_user_actions_reply(db):
    reg = ToolRegistry()

    async def _noop(i, u):
        return "ok"
    reg.register(Tool(definition=ToolDef(name="create_task", description="", input_schema={}),
                      handler=_noop))
    companion = RecordingAgent(outcome=AgentOutcome(
        text="made it!",
        actions=[ExecutedAction("create_task", {"title": "x"}, "created", False, False)],
    ))
    orch = _orch({"chordial": companion}, tool_registry=reg)
    run(orch.handle(Stimulus(kind="user_message", user_uuid="u1", platform="discord",
                             content="make a task", user_name="dain", user_timezone="UTC")))
    assert [(k, at) for k, at, _, _ in _events(db)] == [
        ("message", "user"), ("action", "agent"), ("message", "agent"),
    ]


def test_provider_error_records_nothing_and_flags_errored(db):
    class ExplodingAgent:
        name = "chordial"

        async def act(self, briefing):
            raise ProviderError("api down")

    orch = _orch({"chordial": ExplodingAgent()})
    d = run(orch.handle(Stimulus(kind="user_message", user_uuid="u1", platform="discord",
                                 content="hello?", user_name="dain", user_timezone="UTC")))
    assert d.errored is True
    # only the inbound user message was persisted
    assert [k for k, _, _, _ in _events(db)] == ["message"]


def test_curator_outcome_is_silent_and_unrecorded(db):
    orch = _orch({"curator": FakeCurator()})
    d = run(orch.handle(Stimulus(kind="curation_due", user_uuid="u1")))
    assert d.text is None
    assert _events(db) == []  # nothing written to the conversation


# --- tool registry view ----------------------------------------------------------

def test_registry_view_filters_and_shares_tools():
    reg = ToolRegistry()

    async def _noop(i, u):
        return "ok"
    reg.register(Tool(definition=ToolDef(name="a", description="", input_schema={}), handler=_noop))
    reg.register(Tool(definition=ToolDef(name="b", description="", input_schema={}), handler=_noop,
                      terminal=True))

    view = reg.view(["b"])
    assert [d.name for d in view.definitions()] == ["b"]
    assert view.is_terminal("b") is True
    assert reg.view(["a", "b"]).definitions() and len(reg.view(["a", "b"]).definitions()) == 2


def test_registry_view_raises_on_unknown_tool():
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.view(["ghost_tool"])
