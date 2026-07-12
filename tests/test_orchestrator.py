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
import src.services.orchestrator as orch_mod  # noqa: E402
from src.services.orchestrator import Orchestrator, Stimulus  # noqa: E402
from src.services.tools.base import Tool, ToolRegistry  # noqa: E402
from src.providers.ai.types import ToolDef, ProviderError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
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


class FakeView:
    def __init__(self, helper_id, is_active=True):
        self.helper_id = helper_id
        self.is_active = is_active


class FakeHSM:
    """a fixed active cast; chordial is always present, like the real manager."""

    def __init__(self, active=("chordial",)):
        ids = list(active)
        if "chordial" not in ids:
            ids.insert(0, "chordial")
        self._ids = ids

    async def active_helpers(self, user_uuid):
        return [FakeView(h, True) for h in self._ids]


class FakeDeliver:
    """the speaker-aware out-of-band send (router.deliver_as in prod)."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []  # (platform, target_id, text, speaker)

    async def __call__(self, platform, target_id, text, speaker):
        self.calls.append((platform, target_id, text, speaker))
        return self.ok


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
        return [
            (e.kind, e.author_type, e.author, e.message_type)
            for e in s.query(ConversationEvent).order_by(ConversationEvent.id).all()
        ]


# --- selection ---------------------------------------------------------------


def test_user_message_goes_to_companion(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    d = run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="discord",
                content="hello",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    assert d.text == "hi!"
    assert len(companion.briefings) == 1
    assert companion.briefings[0].kind == "user_message"


def test_scheduled_tick_goes_to_companion_with_checkin_briefing(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    run(
        orch.handle(Stimulus(kind="scheduled_tick", user_uuid="u1", platform="discord"))
    )
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
    run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="discord",
                content="the new message",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    events = companion.briefings[0].events
    assert events[-1].content == "the new message"
    assert events[-1].role == "user"


def test_scheduler_stimulus_resolves_profile_from_db(db):
    """the scheduler doesn't resolve names/timezones; the orchestrator must."""
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    run(
        orch.handle(Stimulus(kind="scheduled_tick", user_uuid="u1", platform="discord"))
    )
    b = companion.briefings[0]
    assert b.user_name == "dain"
    assert b.user_timezone == "US/Pacific"


# --- recording -----------------------------------------------------------------


def test_scheduled_reply_is_recorded_only_after_delivery_confirmation(db):
    companion = RecordingAgent(outcome=AgentOutcome(text="checking in~"))
    orch = _orch({"chordial": companion})
    d = run(
        orch.handle(Stimulus(kind="scheduled_tick", user_uuid="u1", platform="discord"))
    )
    assert d.text == "checking in~"
    assert _events(db) == []

    run(
        orch.record_delivered_message(
            user_uuid="u1",
            platform="discord",
            speaker="chordial",
            text=d.text,
            message_type="scheduled",
        )
    )
    assert _events(db) == [("message", "agent", "chordial", "scheduled")]


def test_record_order_user_actions_reply(db):
    reg = ToolRegistry()

    async def _noop(i, u):
        return "ok"

    reg.register(
        Tool(
            definition=ToolDef(name="create_task", description="", input_schema={}),
            handler=_noop,
        )
    )
    companion = RecordingAgent(
        outcome=AgentOutcome(
            text="made it!",
            actions=[
                ExecutedAction("create_task", {"title": "x"}, "created", False, False)
            ],
        )
    )
    orch = _orch({"chordial": companion}, tool_registry=reg)
    run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="discord",
                content="make a task",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    assert [(k, at) for k, at, _, _ in _events(db)] == [
        ("message", "user"),
        ("action", "agent"),
        ("message", "agent"),
    ]


def test_provider_error_records_nothing_and_flags_errored(db):
    class ExplodingAgent:
        name = "chordial"

        async def act(self, briefing):
            raise ProviderError("api down")

    orch = _orch({"chordial": ExplodingAgent()})
    d = run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="discord",
                content="hello?",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
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

    reg.register(
        Tool(
            definition=ToolDef(name="a", description="", input_schema={}), handler=_noop
        )
    )
    reg.register(
        Tool(
            definition=ToolDef(name="b", description="", input_schema={}),
            handler=_noop,
            terminal=True,
        )
    )

    view = reg.view(["b"])
    assert [d.name for d in view.definitions()] == ["b"]
    assert view.is_terminal("b") is True
    assert (
        reg.view(["a", "b"]).definitions()
        and len(reg.view(["a", "b"]).definitions()) == 2
    )


def test_registry_view_raises_on_unknown_tool():
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.view(["ghost_tool"])


# --- group delivery, scope, and dm privacy (v3) -------------------------------


def _events_meta(db):
    with db() as s:
        return [
            (e.author, e.kind, dict(e.event_metadata or {}))
            for e in s.query(ConversationEvent).order_by(ConversationEvent.id).all()
        ]


def test_group_delivers_each_line_out_of_band_with_a_gap(db, monkeypatch):
    """a group activation delivers every line through the speaker-aware router
    (each bot speaks for itself), with one natural gap between the two lines,
    and returns handled=True/text=None so the interface sends nothing."""
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(orch_mod.asyncio, "sleep", fake_sleep)

    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="train time"))
    aria = RecordingAgent(name="aria", outcome=AgentOutcome(text="a poem"))
    deliver = FakeDeliver()
    orch = _orch(
        {"chordial": RecordingAgent(), "tempo": tempo, "aria": aria},
        helper_state_manager=FakeHSM(("chordial", "tempo", "aria")),
        deliver=deliver,
    )
    d = run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="@tempo @aria hey",
                chat_scope="group",
                group_chat_id="g1",
                mentioned=["tempo", "aria"],
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )

    assert deliver.calls == [
        ("telegram", "g1", "train time", "tempo"),
        ("telegram", "g1", "a poem", "aria"),
    ]
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 5.0  # one gap between two lines
    assert d.handled is True
    assert d.text is None


def test_dm_returns_text_without_out_of_band_delivery(db):
    deliver = FakeDeliver()
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="hey from tempo"))
    orch = _orch({"chordial": RecordingAgent(), "tempo": tempo}, deliver=deliver)
    d = run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="hi tempo",
                chat_scope="dm",
                dm_helper="tempo",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    assert d.text == "hey from tempo"
    assert d.handled is False
    assert (
        deliver.calls == []
    )  # dm: the receiving interface sends, not the orchestrator


def test_dm_with_target_records_only_after_router_confirms_delivery(db):
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="hey from tempo"))
    failed = FakeDeliver(ok=False)
    orch = _orch({"chordial": RecordingAgent(), "tempo": tempo}, deliver=failed)
    d = run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="hi tempo",
                delivery_target_id="42",
                chat_scope="dm",
                dm_helper="tempo",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    assert d.errored is True and d.handled is False
    assert _events(db) == [("message", "user", "user", "conversation")]

    delivered = FakeDeliver(ok=True)
    orch.deliver = delivered
    d = run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="try again",
                delivery_target_id="42",
                chat_scope="dm",
                dm_helper="tempo",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    assert d.handled is True
    assert _events(db)[-1] == ("message", "agent", "tempo", "conversation")


def test_group_without_deliver_hook_reports_error_and_does_not_record_reply(
    db, monkeypatch
):
    monkeypatch.setattr(orch_mod.asyncio, "sleep", lambda s: asyncio.sleep(0))
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="hi"))
    orch = _orch(
        {"chordial": RecordingAgent(), "tempo": tempo},
        helper_state_manager=FakeHSM(("chordial", "tempo")),
    )  # deliver=None
    d = run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="@tempo hi",
                chat_scope="group",
                group_chat_id="g1",
                mentioned=["tempo"],
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    assert d.handled is False and d.errored is True and d.text is None
    assert _events(db) == [("message", "user", "user", "conversation")]


def test_dm_events_carry_the_helpers_private_scope(db):
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="ok"))
    orch = _orch({"chordial": RecordingAgent(), "tempo": tempo})
    run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="hi",
                chat_scope="dm",
                dm_helper="tempo",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    metas = _events_meta(db)
    assert [(a, k) for a, k, _ in metas] == [("user", "message"), ("tempo", "message")]
    assert all(
        m.get("scope") == "dm" and m.get("with_helper") == "tempo" for _, _, m in metas
    )


def test_group_events_carry_no_scope_tag(db, monkeypatch):
    """group history writes no scope tag (absence means group), keeping those
    bytes identical to pre-dm history."""
    monkeypatch.setattr(orch_mod.asyncio, "sleep", lambda s: asyncio.sleep(0))
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="ok"))
    orch = _orch(
        {"chordial": RecordingAgent(), "tempo": tempo},
        helper_state_manager=FakeHSM(("chordial", "tempo")),
        deliver=FakeDeliver(),
    )
    run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="@tempo hi",
                chat_scope="group",
                group_chat_id="g1",
                mentioned=["tempo"],
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    assert all(not m for _, _, m in _events_meta(db))  # no scope metadata anywhere


def test_briefing_is_scope_aware_and_excludes_a_siblings_dm(db):
    """the privacy window: chordial sees the group channel and its own dm, but
    never aria's private transcript. (verifies the behavior the foundation's
    recent(visible_to=) is meant to provide; the orchestrator filters here
    because that foundation branch has a detached-instance bug - see the
    _visible_window docstring.)"""
    from src.managers.event_log import EventLog

    log = EventLog("u1")
    log.append_message(
        "user",
        "user",
        "secret for aria",
        platform="telegram",
        scope="dm",
        with_helper="aria",
    )
    log.append_message(
        "agent",
        "aria",
        "aria private reply",
        platform="telegram",
        scope="dm",
        with_helper="aria",
    )
    log.append_message(
        "user", "user", "group hello", platform="telegram", scope="group"
    )

    companion = RecordingAgent()  # chordial
    orch = _orch({"chordial": companion})
    run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="hi chordial",
                chat_scope="dm",
                dm_helper="chordial",
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )

    seen = [e.content for e in companion.briefings[0].events]
    assert "secret for aria" not in seen
    assert "aria private reply" not in seen
    assert "group hello" in seen  # the shared channel is visible
    assert "hi chordial" in seen  # its own inbound dm is visible


def test_group_briefing_reflects_scope_and_cue(db, monkeypatch):
    """the director's stage direction (cue/style) and the group scope reach the
    briefed agent."""
    monkeypatch.setattr(orch_mod.asyncio, "sleep", lambda s: asyncio.sleep(0))
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="ok"))
    orch = _orch(
        {"chordial": RecordingAgent(), "tempo": tempo},
        helper_state_manager=FakeHSM(("chordial", "tempo")),
        deliver=FakeDeliver(),
    )
    run(
        orch.handle(
            Stimulus(
                kind="user_message",
                user_uuid="u1",
                platform="telegram",
                content="@tempo hi",
                chat_scope="group",
                group_chat_id="g1",
                mentioned=["tempo"],
                user_name="dain",
                user_timezone="UTC",
            )
        )
    )
    b = tempo.briefings[0]
    assert b.scope == "group"
    assert b.style == "full"
