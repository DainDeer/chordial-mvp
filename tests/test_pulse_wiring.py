"""vel's pulse wiring: the source's beats, the factory's plans, the
gate stack's vel-specific members, and ambient ticks end-to-end through
the real engine against a real temp db.

the rhythm/gate/store arithmetic itself is library-tested in the dainframe;
what's vel's to lock down is the COMPOSITION - who gets which beats,
what a plan resolves, what curation is exempt from, and that a tick lands a
scheduled message through the same delivery path as every chat message.
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import (  # noqa: E402
    Base,
    ConversationEvent,
    PlatformIdentity,
    User,
)
from src.agents import AgentOutcome  # noqa: E402
from src.managers.event_log import EventLog  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.orchestration import build_orchestrator  # noqa: E402
from src.services.pulse_wiring import (  # noqa: E402
    CHECKIN_RHYTHM,
    CURATION_RHYTHM,
    ChordialPulseSource,
    ChordialStimulusFactory,
    OnboardingGate,
    ScheduledOnly,
    build_pulse,
    checkin_rhythm,
    curation_rhythm,
)
from dainframe.core import DeliveryTarget  # noqa: E402
from dainframe.pulse import FiringPlan, RhythmDecision, RhythmKey  # noqa: E402

# noon US/Pacific in june (PDT, utc-7): comfortably outside quiet hours
NOW = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def naive(dt):
    """vel's db rows are naive utc."""
    return dt.replace(tzinfo=None)


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
        s.add(PlatformIdentity(
            user_uuid="u1", platform="discord", platform_user_id="42",
        ))
        s.commit()
    yield TestSession
    engine.dispose()


def seed_message(db, author_type, author, content, at, message_type="conversation"):
    with db() as s:
        s.add(ConversationEvent(
            user_uuid="u1", platform="discord", author_type=author_type,
            author=author, kind="message", content=content,
            message_type=message_type, created_at=naive(at),
        ))
        s.commit()


class RecordingAgent:
    def __init__(self, name="vel", outcome=None):
        self.name = name
        self.outcome = outcome or AgentOutcome(text="checking in~")
        self.briefings = []

    async def act(self, briefing):
        self.briefings.append(briefing)
        return self.outcome


class FakeCurator:
    name = "curator"

    def __init__(self, users=("u1",)):
        self.users = list(users)
        self.curated = []

    async def find_users_needing_curation(self):
        return self.users

    async def act(self, briefing):
        self.curated.append(briefing.stream_id)
        return AgentOutcome(text=None)


class FakeDeliver:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def __call__(self, platform, target_id, text, speaker="vel"):
        self.calls.append((platform, target_id, text, speaker))
        return self.ok


def make_pulse(db, *, companion=None, curator=None, deliver=None):
    companion = companion or RecordingAgent()
    deliver = deliver or FakeDeliver()
    agents = {"vel": companion}
    if curator is not None:
        agents["curator"] = curator
    orch = build_orchestrator(
        agents=agents, user_manager=UserManager(), deliver=deliver
    )
    pulse = build_pulse(
        orchestrator=orch,
        user_manager=UserManager(),
        curator=curator,
        platforms=["discord"],
        now=lambda: NOW,
    )
    return pulse, companion, deliver, orch


# --- the source: who carries which beats -------------------------------------


def test_source_lists_checkin_and_curation_beats(db):
    source = ChordialPulseSource(UserManager(), curator=FakeCurator())
    streams = dict(run(source.streams()))
    assert [r.rhythm_id for r in streams["u1"]] == [CHECKIN_RHYTHM, CURATION_RHYTHM]
    assert [r.kind for r in streams["u1"]] == ["scheduled_tick", "curation_due"]


def test_curation_discovery_failure_never_stalls_checkins(db):
    class BrokenCurator:
        async def find_users_needing_curation(self):
            raise RuntimeError("notion is down")

    source = ChordialPulseSource(UserManager(), curator=BrokenCurator())
    streams = dict(run(source.streams()))
    assert [r.rhythm_id for r in streams["u1"]] == [CHECKIN_RHYTHM]


# --- the factory: plans resolve WHERE before any tokens ----------------------


def test_checkin_plan_resolves_delivery_and_carries_staleness(db):
    factory = ChordialStimulusFactory(
        UserManager(), platforms=["discord"], now=lambda: NOW
    )
    plan = run(factory.plan("u1", checkin_rhythm(), RhythmDecision(due_at=NOW)))
    assert plan.actor == "vel"
    assert plan.target == DeliveryTarget(platform="discord", target_id="42")
    assert plan.precondition is not None

    stimulus = run(factory.build(plan))
    assert stimulus.kind == "scheduled_tick"
    assert (stimulus.scope, stimulus.audience) == ("dm", "vel")
    assert stimulus.record_inbound is False
    assert stimulus.precondition is plan.precondition


def test_no_deliverable_platform_means_no_firing(db):
    factory = ChordialStimulusFactory(
        UserManager(), platforms=["telegram"], now=lambda: NOW  # no tg link
    )
    assert run(
        factory.plan("u1", checkin_rhythm(), RhythmDecision(due_at=NOW))
    ) is None


def test_curation_plan_is_minimal(db):
    factory = ChordialStimulusFactory(UserManager(), now=lambda: NOW)
    plan = run(factory.plan("u1", curation_rhythm(), RhythmDecision(due_at=NOW)))
    assert plan.target is None and plan.actor is None
    stimulus = run(factory.build(plan))
    assert stimulus.kind == "curation_due"
    assert stimulus.record_inbound is False


# --- the factory: presence-aware routing (ROOMS_DESIGN section 9, 7a) --------


def _link(db, platform, platform_user_id):
    with db() as s:
        s.add(PlatformIdentity(
            user_uuid="u1", platform=platform,
            platform_user_id=platform_user_id,
        ))
        s.commit()


def _plan_target(presence, platforms, db):
    factory = ChordialStimulusFactory(
        UserManager(), platforms=platforms, now=lambda: NOW,
        presence=presence,
    )
    plan = run(factory.plan("u1", checkin_rhythm(), RhythmDecision(due_at=NOW)))
    return plan.target if plan else None


def test_active_presence_routes_the_checkin_to_the_desk(db):
    """someone demonstrably at the desk gets the desktop, even though they
    last SPOKE on discord - presence beats recency."""
    _link(db, "app", "u1")
    _link(db, "telegram", "777")
    seed_message(db, "user", "user", "hi", NOW - timedelta(hours=2))

    target = _plan_target(lambda u: "active",
                          ["discord", "telegram", "app"], db)
    assert target == DeliveryTarget(platform="app", target_id="u1")


def test_idle_presence_prefers_the_phone(db):
    """away or idle -> the tether: the desk surface may be connected, but
    nobody is at it."""
    _link(db, "app", "u1")
    _link(db, "telegram", "777")
    seed_message(db, "user", "user", "hi", NOW - timedelta(hours=2))

    target = _plan_target(lambda u: "idle",
                          ["discord", "telegram", "app"], db)
    assert target is not None
    assert target.platform != "app"      # never the unattended desk first


def test_idle_with_no_phone_falls_back_to_the_connected_desk(db):
    """no messaging platform linked, but a desk surface is connected: the
    word waits on screen rather than going unsaid."""
    with db() as s:
        s.query(PlatformIdentity).delete()   # drop the seeded discord link
        s.commit()
    _link(db, "app", "u1")

    target = _plan_target(lambda u: "idle", ["discord", "telegram", "app"], db)
    assert target == DeliveryTarget(platform="app", target_id="u1")


def test_absent_presence_never_targets_the_closed_app(db):
    """the app is closed: a send there could never confirm, so targeting is
    exactly the pre-7a messaging-platform behavior."""
    _link(db, "app", "u1")

    target = _plan_target(lambda u: "absent",
                          ["discord", "telegram", "app"], db)
    assert target == DeliveryTarget(platform="discord", target_id="42")


def test_no_presence_source_reads_as_absent(db):
    _link(db, "app", "u1")
    target = _plan_target(None, ["discord", "telegram", "app"], db)
    assert target == DeliveryTarget(platform="discord", target_id="42")


def test_presence_failure_costs_the_desk_preference_never_the_checkin(db):
    def broken(user_uuid):
        raise RuntimeError("presence source down")

    target = _plan_target(broken, ["discord", "telegram", "app"], db)
    assert target == DeliveryTarget(platform="discord", target_id="42")


# --- the gates: vel's members and exemptions ----------------------------


def test_onboarding_gate_blocks_outreach_but_never_curation(db):
    with db() as s:
        s.add(User(uuid="u2", preferred_name=None))
        s.commit()
    gate = ScheduledOnly(OnboardingGate(UserManager()))

    def firing(kind):
        return FiringPlan(
            key=RhythmKey(stream_id="u2", rhythm_id="x"), kind=kind,
            due_at=NOW, actor="vel",
        )

    outreach = run(gate.check(firing("scheduled_tick"), None, NOW))
    assert not outreach.allowed
    assert "onboarding" in outreach.reason
    assert run(gate.check(firing("curation_due"), None, NOW)).allowed


# --- end-to-end: ambient ticks through the real engine -----------------------


def test_pulse_tick_delivers_a_checkin_end_to_end(db):
    seed_message(db, "user", "user", "hi", NOW - timedelta(hours=2))
    pulse, companion, deliver, orch = make_pulse(db)

    run(pulse.tick())

    # delivered through the same speaker-aware path as chat, recorded as a
    # scheduled dm from vel
    assert deliver.calls == [("discord", "42", "checking in~", "vel")]
    assert companion.briefings[0].kind == "scheduled_checkin"
    events = EventLog("u1").recent()
    assert [(e.author, e.message_type) for e in events] == [
        ("user", "conversation"), ("vel", "scheduled"),
    ]

    # immediately after: the horizon holds, nothing re-fires
    run(pulse.tick())
    assert len(deliver.calls) == 1


def test_stale_checkin_cancels_before_generation(db):
    """the user speaking between plan and activation makes the check-in
    stale: the engine's precondition cancels it with zero tokens spent."""
    seed_message(db, "user", "user", "hi", NOW - timedelta(hours=2))
    companion = RecordingAgent()
    deliver = FakeDeliver()
    orch = build_orchestrator(
        agents={"vel": companion}, user_manager=UserManager(),
        deliver=deliver,
    )
    factory = ChordialStimulusFactory(
        UserManager(), platforms=["discord"], now=lambda: NOW
    )
    plan = run(factory.plan("u1", checkin_rhythm(), RhythmDecision(due_at=NOW)))

    # ...the user speaks after the plan was made (real utcnow: newer than NOW)
    EventLog("u1").append_message("user", "user", "actually hey!",
                                  message_type="conversation", platform="discord")

    result = run(orch.handle(run(factory.build(plan))))
    assert result.status == "cancelled"
    assert companion.briefings == []
    assert deliver.calls == []


def test_curation_beat_runs_silently_through_the_engine(db):
    # recent user message: the CHECK-IN is not due, but curation is
    seed_message(db, "user", "user", "hi", NOW - timedelta(minutes=1))
    curator = FakeCurator()
    pulse, companion, deliver, orch = make_pulse(db, curator=curator)

    run(pulse.tick())

    assert curator.curated == ["u1"]      # the curation activation ran
    assert deliver.calls == []            # and nothing was said out loud
    assert companion.briefings == []
