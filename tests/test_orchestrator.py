"""engine-integration tests: the dainframe engine wired with chordial's
director/context/hooks/store (src/services/orchestration.py), driven end to
end with fake agents against a real temp db.

the engine's own invariants are tested in the dainframe; these lock down the
CHORDIAL wiring: the record order (user -> actions -> reply), confirmed-send-
before-history through the router adapter, the scheduled pending flow, dm
scope tagging and the privacy window, group multi-speaker delivery with the
breathing gap, and profile resolution for scheduler-shaped stimuli.
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
from src.agents import AgentOutcome, Briefing  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from dainframe.core import DeliveryReceipt, DeliveryTarget, Stimulus  # noqa: E402
from dainframe.loop.agent_loop import ExecutedAction  # noqa: E402
from dainframe.providers.types import ProviderError  # noqa: E402
import src.services.orchestration as orch_mod  # noqa: E402
from src.services.orchestration import build_orchestrator  # noqa: E402
from src.services.tools import Tool, ToolRegistry  # noqa: E402
from dainframe.providers.types import ToolDef  # noqa: E402


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

    async def __call__(self, platform, target_id, text, speaker="chordial"):
        self.calls.append((platform, target_id, text, speaker))
        return self.ok


class FakeCurator:
    name = "curator"

    def __init__(self):
        self.curated: list[str] = []

    async def find_users_needing_curation(self):
        return ["u1"]

    async def act(self, briefing):
        self.curated.append(briefing.stream_id)
        return AgentOutcome(text=None)


def _orch(agents, deliver=None, **kwargs):
    return build_orchestrator(
        agents=agents, user_manager=UserManager(), deliver=deliver, **kwargs
    )


def _dm(content, helper="chordial", platform="discord", target_id="42"):
    return Stimulus(
        kind="user_message", stream_id="u1", content=content, platform=platform,
        scope="dm", audience=helper, addressed=(helper,),
        target=DeliveryTarget(platform=platform, target_id=target_id),
        extras={"user_name": "dain", "user_timezone": "UTC"},
    )


def _group(content, mentioned, platform="telegram", chat="g1"):
    return Stimulus(
        kind="user_message", stream_id="u1", content=content, platform=platform,
        scope="group", addressed=tuple(mentioned),
        target=DeliveryTarget(platform=platform, target_id=chat),
        extras={"user_name": "dain", "user_timezone": "UTC"},
    )


def _tick(platform="discord", target_id="42"):
    return Stimulus(
        kind="scheduled_tick", stream_id="u1", platform=platform,
        record_inbound=False, scope="dm", audience="chordial",
        target=DeliveryTarget(platform=platform, target_id=target_id),
    )


def _events(db):
    with db() as s:
        return [
            (e.kind, e.author_type, e.author, e.message_type)
            for e in s.query(ConversationEvent).order_by(ConversationEvent.id).all()
        ]


# --- selection & briefing ------------------------------------------------------


def test_user_message_goes_to_companion(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion}, deliver=FakeDeliver())
    result = run(orch.handle(_dm("hello")))
    assert result.lines[0].status == "delivered"
    assert len(companion.briefings) == 1
    assert companion.briefings[0].kind == "user_message"


def test_scheduled_tick_briefs_a_checkin(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    run(orch.handle(_tick()))
    assert companion.briefings[0].kind == "scheduled_checkin"


def test_curation_due_goes_to_curator(db):
    curator = FakeCurator()
    orch = _orch({"chordial": RecordingAgent(), "curator": curator})
    run(orch.handle(Stimulus(kind="curation_due", stream_id="u1",
                             record_inbound=False)))
    assert curator.curated == ["u1"]


def test_unknown_stimulus_is_an_explicit_noop(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    result = run(orch.handle(Stimulus(kind="mystery", stream_id="u1")))
    assert result.status == "noop"
    assert companion.briefings == []


def test_briefing_includes_inbound_message_as_last_event(db):
    companion = RecordingAgent()
    orch = _orch({"chordial": companion}, deliver=FakeDeliver())
    run(orch.handle(_dm("the new message")))
    events = companion.briefings[0].events
    assert events[-1].content == "the new message"
    assert events[-1].author_type == "user"


def test_scheduler_stimulus_resolves_profile_from_db(db):
    """the scheduler doesn't resolve names/timezones; ChordialContext must."""
    companion = RecordingAgent()
    orch = _orch({"chordial": companion})
    run(orch.handle(_tick()))
    b = companion.briefings[0]
    assert b.extras["user_name"] == "dain"
    assert b.extras["user_timezone"] == "US/Pacific"


# --- recording -----------------------------------------------------------------


def test_scheduled_reply_is_recorded_only_after_delivery_confirmation(db):
    companion = RecordingAgent(outcome=AgentOutcome(text="checking in~"))
    orch = _orch({"chordial": companion})
    result = run(orch.handle(_tick()))

    (line,) = result.lines
    assert line.status == "pending"
    assert line.pending.text == "checking in~"
    assert _events(db) == []          # nothing in history until confirmed

    run(orch.confirm_delivery(line.pending.pending_id, DeliveryReceipt()))
    assert _events(db) == [("message", "agent", "chordial", "scheduled")]

    # confirming twice records once (the engine's idempotence invariant)
    run(orch.confirm_delivery(line.pending.pending_id, DeliveryReceipt()))
    assert len(_events(db)) == 1


def test_record_order_user_actions_reply(db):
    companion = RecordingAgent(
        outcome=AgentOutcome(
            text="made it!",
            actions=(
                ExecutedAction("create_task", {"title": "x"}, "created",
                               False, False, True),
            ),
        )
    )
    orch = _orch({"chordial": companion}, deliver=FakeDeliver())
    run(orch.handle(_dm("make a task")))
    assert [(k, at) for k, at, _, _ in _events(db)] == [
        ("message", "user"),
        ("action", "agent"),
        ("message", "agent"),
    ]


def test_provider_error_records_nothing_and_errors_the_line(db):
    class ExplodingAgent:
        name = "chordial"

        async def act(self, briefing):
            raise ProviderError("api down")

    orch = _orch({"chordial": ExplodingAgent()}, deliver=FakeDeliver())
    result = run(orch.handle(_dm("hello?")))
    assert result.lines[0].status == "errored"
    # only the inbound user message was persisted
    assert [k for k, _, _, _ in _events(db)] == ["message"]


def test_curator_outcome_is_silent_and_unrecorded(db):
    orch = _orch({"curator": FakeCurator(), "chordial": RecordingAgent()})
    result = run(orch.handle(Stimulus(kind="curation_due", stream_id="u1",
                                      record_inbound=False)))
    assert result.lines[0].status == "silent"
    assert _events(db) == []  # nothing written to the conversation


# --- group delivery, scope, and dm privacy -------------------------------------


def _events_meta(db):
    with db() as s:
        return [
            (e.author, e.kind, dict(e.event_metadata or {}))
            for e in s.query(ConversationEvent).order_by(ConversationEvent.id).all()
        ]


def test_group_delivers_each_line_out_of_band_with_a_gap(db, monkeypatch):
    """a group activation delivers every line through the speaker-aware router
    (each bot speaks for itself), with one natural gap between the two lines."""
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(orch_mod.asyncio, "sleep", fake_sleep)

    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="train time"))
    aria = RecordingAgent(name="aria", outcome=AgentOutcome(text="a poem"))
    deliver = FakeDeliver()
    orch = _orch(
        {"chordial": RecordingAgent(), "tempo": tempo, "aria": aria},
        deliver=deliver,
        helper_state_manager=FakeHSM(("chordial", "tempo", "aria")),
    )
    result = run(orch.handle(_group("@tempo @aria hey", ["tempo", "aria"])))

    assert deliver.calls == [
        ("telegram", "g1", "train time", "tempo"),
        ("telegram", "g1", "a poem", "aria"),
    ]
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 5.0  # one gap between two lines
    assert [line.status for line in result.lines] == ["delivered", "delivered"]


def test_dm_without_target_is_a_configuration_error(db):
    """the old 'no delivery hook -> return the text and record it anyway'
    fallback is deliberately dead (dainframe DESIGN.md §11.3): an activation
    that can't confirm a send never pretends it delivered."""
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="hey"))
    orch = _orch({"chordial": RecordingAgent(), "tempo": tempo},
                 deliver=FakeDeliver())
    stim = Stimulus(
        kind="user_message", stream_id="u1", content="hi tempo",
        platform="telegram", scope="dm", audience="tempo", addressed=("tempo",),
        extras={"user_name": "dain", "user_timezone": "UTC"},  # no target
    )
    result = run(orch.handle(stim))
    assert result.lines[0].status == "errored"
    assert result.lines[0].error.kind == "delivery_configuration"
    assert _events(db) == [("message", "user", "user", "conversation")]


def test_dm_records_only_after_router_confirms_delivery(db):
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="hey from tempo"))
    failed = FakeDeliver(ok=False)
    orch = _orch({"chordial": RecordingAgent(), "tempo": tempo}, deliver=failed)
    result = run(orch.handle(_dm("hi tempo", helper="tempo", platform="telegram")))
    assert result.lines[0].status == "errored"
    assert result.lines[0].error.kind == "delivery_failed"
    assert _events(db) == [("message", "user", "user", "conversation")]

    delivered = FakeDeliver(ok=True)
    orch2 = _orch({"chordial": RecordingAgent(), "tempo": tempo}, deliver=delivered)
    result = run(orch2.handle(_dm("try again", helper="tempo", platform="telegram")))
    assert result.any_delivered is True
    assert _events(db)[-1] == ("message", "agent", "tempo", "conversation")


def test_dm_events_carry_the_helpers_private_scope(db):
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="ok"))
    orch = _orch({"chordial": RecordingAgent(), "tempo": tempo},
                 deliver=FakeDeliver())
    run(orch.handle(_dm("hi", helper="tempo", platform="telegram")))
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
        deliver=FakeDeliver(),
        helper_state_manager=FakeHSM(("chordial", "tempo")),
    )
    run(orch.handle(_group("@tempo hi", ["tempo"])))
    assert all(not m for _, _, m in _events_meta(db))  # no scope metadata anywhere


def test_briefing_is_scope_aware_and_excludes_a_siblings_dm(db):
    """the privacy window: chordial sees the group channel and its own dm, but
    never aria's private transcript."""
    from src.managers.event_log import EventLog

    log = EventLog("u1")
    log.append_message("user", "user", "secret for aria",
                       platform="telegram", scope="dm", with_helper="aria")
    log.append_message("agent", "aria", "aria private reply",
                       platform="telegram", scope="dm", with_helper="aria")
    log.append_message("user", "user", "group hello",
                       platform="telegram", scope="group")

    companion = RecordingAgent()  # chordial
    orch = _orch({"chordial": companion}, deliver=FakeDeliver())
    run(orch.handle(_dm("hi chordial", platform="telegram")))

    seen = [e.content for e in companion.briefings[0].events]
    assert "secret for aria" not in seen
    assert "aria private reply" not in seen
    assert "group hello" in seen  # the shared channel is visible
    assert "hi chordial" in seen  # its own inbound dm is visible


def test_group_briefing_reflects_scope(db, monkeypatch):
    monkeypatch.setattr(orch_mod.asyncio, "sleep", lambda s: asyncio.sleep(0))
    tempo = RecordingAgent(name="tempo", outcome=AgentOutcome(text="ok"))
    orch = _orch(
        {"chordial": RecordingAgent(), "tempo": tempo},
        deliver=FakeDeliver(),
        helper_state_manager=FakeHSM(("chordial", "tempo")),
    )
    run(orch.handle(_group("@tempo hi", ["tempo"])))
    b = tempo.briefings[0]
    assert b.scope == "group"
    assert b.style == "full"


# --- empty outcomes never fall through silently -------------------------------


def test_empty_outcome_on_user_message_errors_the_line(db):
    """no text, no refusal, no error - on a turn that owes a reply. it must
    surface as an errored line so the caller's graceful fallback fires."""
    companion = RecordingAgent(outcome=AgentOutcome(text=None))
    orch = _orch({"chordial": companion}, deliver=FakeDeliver())
    result = run(orch.handle(_dm("hello?")))
    assert result.lines[0].status == "errored"
    assert result.lines[0].error.kind == "empty_required_reply"


def test_curator_silence_is_still_not_an_error(db):
    orch = _orch({"curator": FakeCurator(), "chordial": RecordingAgent()})
    result = run(orch.handle(Stimulus(kind="curation_due", stream_id="u1",
                                      record_inbound=False)))
    assert result.lines[0].status == "silent"


def test_empty_scheduled_tick_stays_quiet_not_errored(db):
    """the scheduled line is response='optional': a quiet tick is a non-event."""
    companion = RecordingAgent(outcome=AgentOutcome(text=None))
    orch = _orch({"chordial": companion})
    result = run(orch.handle(_tick()))
    assert result.lines[0].status == "silent"
    assert result.lines[0].pending is None
