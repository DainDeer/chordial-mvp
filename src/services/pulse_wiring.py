"""chordial's ambient wiring: the dainframe pulse replaces SchedulerService.

what the extraction design promised (the-dainframe DESIGN.md §6.5),
delivered: `get_scheduled_users` -> PulseSource; onboarding + quiet hours +
the proactivity gate -> a gate stack (two of the three now library-shipped,
verbatim arithmetic); `resolve_delivery_identity` + the first-contact rule ->
`StimulusFactory.plan`; scheduled delivery moves onto the engine's ordinary
DIRECT path - the pending+confirm two-phase dance is gone, generation,
delivery, and recording are one serialized activation; the piggybacked
curation pass is just a second rhythm.

what the pulse adds that the scheduler never had: a staleness precondition
(a user who speaks while the firing waits for the stream lock cancels the
check-in before a single token is spent) and persisted denial horizons (a
backoff-denied user isn't re-checked every five minutes; the gate says when).

timestamps: chordial stores naive-UTC rows; the pulse normalizes naive as
UTC (dainframe as_utc), and this wiring hands the pulse an AWARE utc clock.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from dainframe.core import (
    DeliveryTarget,
    EventQuery,
    ReadOnlyEventReader,
    Stimulus,
)
from dainframe.pulse import (
    BackoffGate,
    as_utc,
    FiringPlan,
    GateDecision,
    InMemoryPulseStore,
    Interval,
    NoNewerEvent,
    Pulse,
    QuietHoursGate,
    RhythmDecision,
    RhythmKey,
    TaggedRhythm,
)

from config import Config
from src.managers.event_log import EventLog
from src.managers.event_store_adapter import SqlEventStore, SqlUserEvents
from src.managers.user_manager import UserManager
from src.services.orchestration import chordial_visibility

logger = logging.getLogger(__name__)

CHECKIN_RHYTHM = "checkin"
CURATION_RHYTHM = "curation"

# the recency clock: any MESSAGE on any platform (one person on two platforms
# is one schedule slot). action/note rows are invisible by construction - a
# tool action or switch notice never masquerades as "the assistant replied".
ANY_MESSAGE = EventQuery(kinds=frozenset({"message"}))
USER_MESSAGES = EventQuery(
    kinds=frozenset({"message"}), author_types=frozenset({"user"})
)


class UserQuietSince:
    """ActivationPrecondition: holds while the user hasn't spoken ANYWHERE
    since the plan was made. replaces stream-scoped NoNewerEvent staleness -
    rooms made per-stream checks too narrow (the engine hands us the
    stimulus stream's reader, which we deliberately ignore)."""

    def __init__(self, user_uuid: str, than):
        self.user_uuid = user_uuid
        self.than = than

    async def holds(self, events) -> bool:
        last = await asyncio.to_thread(
            lambda: EventLog(self.user_uuid).last_user_message())
        if last is None or last.created_at is None:
            return True
        return as_utc(last.created_at) <= as_utc(self.than)


def aware_utc_now() -> datetime:
    """the pulse boundary is aware-UTC; chordial's own utc_now() stays naive
    for db comparisons."""
    return datetime.now(timezone.utc)


def checkin_rhythm() -> TaggedRhythm:
    """the regular check-in beat. anchored on the last MESSAGE from anyone:
    a fresh user reply restarts the clock, and so does our own outreach -
    with the backoff gate stretching the effective wait on ignored chains
    exactly as before. no anchor at all = first contact, due now (still
    behind quiet hours: an imported user's first hello never lands at 3am)."""
    return TaggedRhythm(
        rhythm_id=CHECKIN_RHYTHM,
        kind="scheduled_tick",
        rhythm=Interval(
            every=timedelta(minutes=Config.DM_INTERVAL_MINUTES),
            anchor=ANY_MESSAGE,
        ),
    )


def curation_rhythm() -> TaggedRhythm:
    """the memory-cleanup beat: due whenever the curator's discovery lists
    the user. the small `every` floor only keeps a failing curation from
    re-firing inside one cycle."""
    return TaggedRhythm(
        rhythm_id=CURATION_RHYTHM,
        kind="curation_due",
        rhythm=Interval(every=timedelta(minutes=5), anchor="last_attempt"),
    )


class ChordialPulseSource:
    """who is ambient this cycle. re-read every cycle, so joining/leaving
    needs no registration dance - exactly the old per-cycle user scan."""

    def __init__(self, user_manager: UserManager, curator=None):
        self.user_manager = user_manager
        self.curator = curator

    async def streams(self):
        rhythms: dict[str, list[TaggedRhythm]] = {}
        for user_uuid in await self.user_manager.get_scheduled_users():
            rhythms.setdefault(user_uuid, []).append(checkin_rhythm())
        if self.curator is not None:
            try:
                for user_uuid in await self.curator.find_users_needing_curation():
                    rhythms.setdefault(user_uuid, []).append(curation_rhythm())
            except Exception:
                # curation discovery must never stall check-ins
                logger.exception("curation discovery failed; skipping this cycle")
        return list(rhythms.items())


class ScheduledOnly:
    """chordial's outreach gates guard OUTREACH. curation is silent internal
    bookkeeping - it runs mid-onboarding, at 3am, and under backoff, exactly
    as the old piggybacked pass did."""

    def __init__(self, gate):
        self.gate = gate

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        if firing.kind != "scheduled_tick":
            return GateDecision(True, "not outreach")
        return await self.gate.check(firing, events, now)


class OnboardingGate:
    """LOAD-BEARING: nothing downstream re-checks onboarding - this gate is
    the only thing keeping scheduled sends away from mid-onboarding users
    (same invariant the scheduler's first check carried)."""

    def __init__(self, user_manager: UserManager):
        self.user_manager = user_manager

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        if await self.user_manager.needs_onboarding(firing.key.stream_id):
            return GateDecision(False, "user is mid-onboarding")
        return GateDecision(True, "clear")


class ChordialStimulusFactory:
    """plan: resolve the delivery destination BEFORE any tokens - targeted at
    the platform the user most recently spoke on, falling back to any other
    live link; nowhere to deliver = no firing. build: the same scheduled_tick
    stimulus the engine has always handled, now carrying a staleness
    precondition revalidated under the stream lock."""

    def __init__(
        self,
        user_manager: UserManager,
        platforms: Optional[List[str]] = None,
        now=aware_utc_now,
    ):
        self.user_manager = user_manager
        self.platforms = platforms
        self._now = now

    async def plan(
        self, stream_id: str, rhythm: TaggedRhythm, decision: RhythmDecision
    ) -> Optional[FiringPlan]:
        key = RhythmKey(stream_id=stream_id, rhythm_id=rhythm.rhythm_id)
        if rhythm.kind == "curation_due":
            return FiringPlan(key=key, kind=rhythm.kind, due_at=decision.due_at)

        active = EventLog(stream_id).active_platform()
        target = await self.user_manager.resolve_delivery_identity(
            stream_id, active, self.platforms
        )
        if target is None:
            logger.debug("no deliverable platform for user %s, skipping", stream_id)
            return None
        platform, platform_user_id = target
        return FiringPlan(
            key=key,
            kind=rhythm.kind,
            due_at=decision.due_at,
            actor="chordial",
            target=DeliveryTarget(platform=platform, target_id=platform_user_id),
            reason=decision.reason,
            # if the user speaks between this plan and the stream lock, the
            # check-in is stale: cancel before generation. USER-level on
            # purpose (not the stimulus stream): with rooms, a message in
            # today's room must cancel a check-in planned against any stream
            precondition=UserQuietSince(stream_id, than=self._now()),
        )

    async def build(self, plan: FiringPlan) -> Stimulus:
        # pulse rhythms are per-USER (the rhythm key stays the user uuid);
        # what changed in phase 2b is WHERE a check-in lands: today's daily
        # room, lazily resolved at fire time - so an overnight tick opens
        # (and thereby closes yesterday's) room exactly like a user message
        # would. curation is not a conversation and keeps the user key.
        user_uuid = plan.key.stream_id
        if plan.kind == "curation_due":
            return Stimulus(
                kind="curation_due",
                stream_id=user_uuid,
                record_inbound=False,
                extras={"user_id": user_uuid},
            )

        from src.services.rooms import get_room_store
        from src.services.workspace.agenda import user_today

        def resolve() -> str:
            room = get_room_store().current_room(
                user_uuid, user_today(user_uuid))
            return room["room_uuid"]
        stream_id = await asyncio.to_thread(resolve)

        return Stimulus(
            kind="scheduled_tick",
            stream_id=stream_id,
            platform=plan.target.platform,
            record_inbound=False,
            scope="dm",
            audience="chordial",
            target=plan.target,
            reason=plan.reason,
            precondition=plan.precondition,
            extras={"user_id": user_uuid},
        )


def build_pulse(
    *,
    orchestrator,
    user_manager: UserManager,
    curator=None,
    platforms: Optional[List[str]] = None,
    store=None,
    now=aware_utc_now,
) -> Pulse:
    """chordial's ambient loop: five-minute cycles, per-user unified recency,
    the don't-nag gate stack (onboarding -> quiet hours -> backoff, first
    denial wins). the pulse store is in-memory: horizons rebuild from the
    event log after a restart, so nothing user-visible is lost (a durable
    PulseStore is a later phase, alongside the other SQL adapters)."""
    gates = [
        ScheduledOnly(OnboardingGate(user_manager)),
        ScheduledOnly(QuietHoursGate(
            Config.QUIET_HOURS_START,
            Config.QUIET_HOURS_END,
            tz_of=user_manager.get_user_timezone,
        )),
        ScheduledOnly(BackoffGate(
            crew_cap=Config.GATE_CREW_CAP,
            per_author_cap=Config.GATE_PER_HELPER_CAP,
            base_interval=timedelta(hours=Config.GATE_BASE_INTERVAL_HOURS),
            proactive_message_type="scheduled",
            window=Config.MAX_HISTORY_MESSAGES,
        )),
    ]
    return Pulse(
        source=ChordialPulseSource(user_manager, curator=curator),
        factory=ChordialStimulusFactory(user_manager, platforms=platforms, now=now),
        engine=orchestrator,
        store=store or InMemoryPulseStore(),
        # rhythm keys are USERS: recency anchors and gate arithmetic must
        # span every room (a reply in today's room answers yesterday's nudge)
        events_for=lambda sid: ReadOnlyEventReader(
            SqlUserEvents(sid, visibility=chordial_visibility)
        ),
        gates=gates,
        now=now,
    )
