"""chordial's wiring of the dainframe engine (phase 3 of the extraction).

what used to be a 600-line bespoke orchestrator is now the library engine
(dainframe.core.Orchestrator) plus four small chordial-shaped parts, each
plugged into a seam the engine defines:

- ChordialDirector   (§4.2)  WHO speaks, with what per-line obligations: the
                             dm/group/mention rules, the curator's silent
                             line, the scheduler's pending line
- ChordialContext    (§4.4)  briefing enrichment: user profile lookup, the
                             agenda's ambient digest, the briefing-kind map
- ChordialHooks      (§4.5)  the platform-switch courtesy + the completion
                             reconciler - product niceties, not engine logic
- ChordialDeliverer  (§4.3)  the router adapter, with the group-chat breathing
                             gap between multi-speaker lines

recording goes through SqlEventStore (the contract-proven adapter), delivery
is confirmed-before-recorded by the engine, and per-stream serialization is
the engine's StreamCoordinator (replacing ChatService's ad-hoc user locks as
the deep guarantee; the chat-service lock remains as an outer courtesy).

the legacy per-message compression hook (ENABLE_COMPRESSION, off by default,
pre-event-log vintage) did not survive the swap - CompressorService remains
for whatever comes next, but nothing invokes it on the chat path.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Iterable, Optional

import uuid

from dainframe.core import (
    BoundedDeliveryLedger,
    BriefingContext,
    DeliveryReceipt,
    DeliveryRequest,
    EventContext,
    EventQuery,
    NewEvent,
    NewPendingDelivery,
    Orchestrator,
    PendingDelivery,
    Script,
    ScriptLine,
    Stimulus,
)
from dainframe.core.events import Event as DfEvent, EventStore
from dainframe.loop.agent_loop import ExecutedAction

from config import Config
from src.managers.event_log import format_action_line
from src.managers.event_store_adapter import SqlEventStore
from src.managers.helper_state_manager import HelperStateManager
from src.managers.user_manager import UserManager

logger = logging.getLogger(__name__)


# the one-time courtesy sent to a platform the conversation just walked away
# from. asterisk stage-direction styling matches the persona's voice; on
# plain-text platforms the asterisks read as deliberate emphasis.
SWITCH_NOTICE = "*(pssst — we're chatting over on {platform} now. see you there 💜)*"


def chordial_visibility(event: DfEvent, viewer: str) -> bool:
    """the dm privacy rule on dainframe events: group events are visible to
    every helper; a dm event only to the helper the 1:1 is with (as its
    partner, or as its own author). mirrors chordial Event.visible_to."""
    if event.scope != "dm":
        return True
    return event.audience == viewer or event.author == viewer


# --- the director (§4.2): rules only this phase -------------------------------


class ChordialDirector:
    """casts the script of speakers for one activation. deterministic this
    phase - phase 3 of chordial's own roadmap replaces the group no-mention
    branch with a cheap utility-model call. the director must never break the
    conversation: every conversational path that empties falls back to
    chordial."""

    def __init__(
        self,
        agent_ids: Iterable[str],
        helper_state_manager: Optional[HelperStateManager] = None,
        fallback: str = "chordial",
    ):
        self.agent_ids = set(agent_ids)
        self.helper_state_manager = helper_state_manager or HelperStateManager()
        self.fallback = fallback

    async def direct(self, stimulus: Stimulus, events) -> Script:
        kind = stimulus.kind
        if kind == "curation_due":
            # the curator acts silently: actions (none today) are the product
            return Script(lines=(
                ScriptLine(speaker="curator", response="silent", delivery="none"),
            ))
        if kind == "scheduled_tick":
            # ambient outreach rides the ordinary DIRECT path (the-dainframe
            # DESIGN.md §11.15): the engine holds the stream across
            # generation, delivery, and recording - one serialized
            # activation, no pending+confirm dance. response stays OPTIONAL:
            # a scheduled tick that generates nothing is a quiet non-event,
            # never a user-facing error
            return self._finalize(stimulus, [
                ScriptLine(
                    speaker=self.fallback,
                    response="optional",
                    delivery="direct",
                    target=stimulus.target,
                    event_context=self._dm_context(
                        stimulus, self.fallback, message_type="scheduled"
                    ),
                ),
            ])
        if kind == "introduction":
            speaker = stimulus.addressed[0] if stimulus.addressed else self.fallback
            return self._finalize(stimulus, [self._dm_line(stimulus, speaker)])
        if kind == "user_message":
            if stimulus.scope != "group":
                # a dm is a private 1:1 - the addressed helper is the lone voice
                speaker = stimulus.addressed[0] if stimulus.addressed else self.fallback
                return self._finalize(stimulus, [self._dm_line(stimulus, speaker)])
            return self._finalize(stimulus, await self._group_lines(stimulus))
        # unknown kind: cast nobody, loudly
        return Script(noop_reason=f"no chordial rule for stimulus kind '{kind}'")

    async def _group_lines(self, stimulus: Stimulus) -> list[ScriptLine]:
        """the group user_message routing rule. @-mentions win (in order,
        deduped, active cast only, capped at 2); otherwise chordial fields it."""
        group_ec = EventContext(platform=stimulus.platform, scope="group")
        if not stimulus.addressed:
            return [ScriptLine(speaker=self.fallback, event_context=group_ec)]
        cast = await self.helper_state_manager.active_helpers(stimulus.stream_id)
        active_ids = {v.helper_id for v in cast if v.is_active}
        lines: list[ScriptLine] = []
        seen: set = set()
        for helper_id in stimulus.addressed:
            if (
                helper_id in seen
                or helper_id not in active_ids
                or helper_id not in self.agent_ids
            ):
                continue
            lines.append(ScriptLine(speaker=helper_id, event_context=group_ec))
            seen.add(helper_id)
            if len(lines) >= 2:
                break
        # every mention was inactive/unknown - don't drop the message on the floor
        return lines or [ScriptLine(speaker=self.fallback, event_context=group_ec)]

    def _dm_line(self, stimulus: Stimulus, speaker: str) -> ScriptLine:
        # the resolved speaker also names the private channel, so the legacy
        # single-helper dm stays visible to chordial's own privacy window
        return ScriptLine(
            speaker=speaker,
            event_context=self._dm_context(stimulus, speaker),
        )

    @staticmethod
    def _dm_context(
        stimulus: Stimulus, speaker: str, message_type: str = "conversation"
    ) -> EventContext:
        return EventContext(
            platform=stimulus.platform,
            scope="dm",
            audience=speaker,
            outbound_message_type=message_type,
        )

    def _finalize(self, stimulus: Stimulus, lines: list[ScriptLine]) -> Script:
        """the director's hard guardrail: cap at 2 lines, recast any speaker
        without an agent to the fallback, and never return empty on a
        conversational path."""
        kept = []
        for line in lines[:2]:
            if line.speaker in self.agent_ids:
                kept.append(line)
        if not kept:
            fallback_ec = (
                EventContext(platform=stimulus.platform, scope="group")
                if stimulus.scope == "group"
                else self._dm_context(stimulus, self.fallback)
            )
            kept = [ScriptLine(speaker=self.fallback, event_context=fallback_ec)]
        return Script(lines=tuple(kept))


# --- briefing enrichment (§4.4) -----------------------------------------------


class ChordialContext:
    """fills the chordial-shaped parts of a briefing: the user profile (one
    query when the caller didn't resolve it - e.g. the scheduler), the agenda
    digest as ambient context, and the stimulus-kind -> briefing-kind map."""

    def __init__(self, user_manager: UserManager, agenda_service=None):
        self.user_manager = user_manager
        self.agenda_service = agenda_service

    async def enrich(self, stimulus: Stimulus, line: ScriptLine) -> BriefingContext:
        # curation needs no conversation context - keep it light
        if stimulus.kind == "curation_due":
            return BriefingContext(kind="curation")

        user_name = stimulus.extras.get("user_name")
        user_timezone = stimulus.extras.get("user_timezone")
        if user_timezone is None:
            user_name, user_timezone = await self.user_manager.get_user_profile(
                stimulus.stream_id
            )

        if stimulus.kind == "introduction":
            briefing_kind = "introduction"
        elif stimulus.kind == "scheduled_tick":
            briefing_kind = "scheduled_checkin"
        else:
            briefing_kind = "user_message"

        return BriefingContext(
            kind=briefing_kind,
            # an introduction stays light (no agenda digest) but keeps the
            # event window, so a returning user's intro sees prior context
            ambient_context=(
                None
                if briefing_kind == "introduction"
                else self._compose_ambient(stimulus.stream_id)
            ),
            extras={
                "user_name": user_name,
                "user_timezone": user_timezone or "UTC",
            },
        )

    def _compose_ambient(self, user_uuid: str) -> Optional[str]:
        """the workspace agenda digest for the volatile 'now' turn. pure db
        reads, fully guarded - any failure degrades to None, i.e. today's
        exact prompt bytes."""
        if not self.agenda_service:
            return None
        try:
            parts = []
            digest = self.agenda_service.get_digest(user_uuid)
            if digest:
                parts.append(digest)
            return "\n\n".join(parts) if parts else None
        except Exception:
            logger.exception("failed composing ambient context; continuing without")
            return None


# --- turn hooks (§4.5): the product niceties ----------------------------------


class ChordialHooks:
    """the platform-switch courtesy and the completion reconciler. both fully
    guarded: a hook failure must never affect the reply the user already got
    (the engine additionally isolates every hook call)."""

    def __init__(
        self,
        user_manager: UserManager,
        reconciler=None,
        deliver=None,
        message_window: Optional[int] = None,
    ):
        self.user_manager = user_manager
        self.reconciler = reconciler
        # (platform, target_id, text, speaker) -> bool; router.deliver_as
        self.deliver = deliver
        self.message_window = message_window or Config.MAX_HISTORY_MESSAGES

    # -- the platform-switch courtesy ------------------------------------------

    async def after_inbound_recorded(
        self,
        stimulus: Stimulus,
        store: EventStore,
        prev_user_event: Optional[DfEvent],
    ) -> None:
        """the conversation just walked from platform A to platform B: send
        the one-time courtesy notice to A. structurally self-deduping - after
        this note, the last user message IS on B, so the trigger can't refire
        until the user speaks on A again. the engine holds the stream lock for
        the whole activation, so the check-then-write pair cannot interleave
        with another turn."""
        try:
            if stimulus.kind != "user_message":
                return
            if self.deliver is None or prev_user_event is None:
                return
            old_platform = prev_user_event.platform
            if not old_platform or old_platform == stimulus.platform:
                return

            # only notify a link we could actually reach
            identity = await self.user_manager.get_identity(
                stimulus.stream_id, old_platform
            )
            if identity is None or not identity[1]:
                return
            platform_user_id = identity[0]

            # belt-and-braces: if a switch note for A was already recorded
            # after A's last user message, another handler beat us to it
            recent = await store.read(EventQuery(message_limit=self.message_window))
            for event in reversed(recent):
                if event.event_id == prev_user_event.event_id:
                    break
                if (
                    event.kind == "note"
                    and event.metadata.get("note_type") == "platform_switch"
                    and event.platform == old_platform
                ):
                    return

            notice = SWITCH_NOTICE.format(platform=stimulus.platform)
            # note first (the at-most-once record), then best-effort delivery -
            # a transient miss is an acceptable cost for a cosmetic courtesy
            await store.append(NewEvent(
                author_type="system", author="system", kind="note",
                content=notice, platform=old_platform,
                metadata={"note_type": "platform_switch", "to": stimulus.platform},
            ))
            delivered = await self.deliver(
                old_platform, platform_user_id, notice, speaker="chordial"
            )
            if not delivered:
                logger.info(
                    "switch notice to %s not delivered (transient or dead link)",
                    old_platform,
                )
        except Exception as e:
            logger.error(
                "platform-switch notice failed for user %s: %s", stimulus.stream_id, e
            )

    # -- the completion reconciler ---------------------------------------------

    async def after_turn(self, stimulus: Stimulus, store: EventStore, result) -> None:
        """after a delivered user turn, reconcile any tasks the user mentioned
        finishing in passing (the companion's warmth can crowd out the
        bookkeeping; this narrow pass catches what it missed). Done marks are
        recorded as chordial's own actions, in the turn's scope, so the replay
        reads coherently next turn."""
        if (
            self.reconciler is None
            or stimulus.kind != "user_message"
            or not stimulus.content
            or not result.any_delivered
        ):
            return
        try:
            recent = await store.read(EventQuery(message_limit=self.message_window))
            # drop the just-recorded inbound message; it's passed separately
            recent = [
                e
                for e in recent
                if not (
                    e.kind == "message"
                    and e.author_type == "user"
                    and e.content == stimulus.content
                )
            ][-6:]
            reconcile_result = await self.reconciler.reconcile(
                user_uuid=stimulus.stream_id,
                platform=stimulus.platform,
                message_text=stimulus.content,
                recent=recent,
            )
            for action in reconcile_result.actions:
                if action.is_error:
                    continue
                await store.append(NewEvent(
                    author_type="agent", author="chordial", kind="action",
                    content=format_action_line(
                        action.name, dict(action.input), action.result_content
                    ),
                    platform=stimulus.platform,
                    scope=stimulus.scope,
                    audience=stimulus.audience,
                    metadata={
                        "tool": action.name,
                        "input": dict(action.input),
                        "result": action.result_content[:1000],
                    },
                ))
        except Exception as e:
            logger.error(
                "completion reconcile failed for user %s: %s", stimulus.stream_id, e
            )


# --- delivery (§4.3): the router adapter --------------------------------------


class _PacedDeliverer:
    """the group-chat breathing gap: a second (or later) delivered line in the
    same activation waits a beat first, so a two-speaker script doesn't land
    as a machine-gun burst. only group scripts cast multiple lines, so the
    pacing is effectively group-only. subclasses implement _send.

    pacing state is keyed PER ACTIVATION (a bounded recent-ids set), so
    another user's interleaved delivery can't make an activation lose its
    gap. the set is small: an activation needs pacing only while its own
    (short, serialized) script is still delivering."""

    _SEEN_CAP = 128

    def __init__(self):
        self._delivered_activations: dict[str, None] = {}  # insertion-ordered

    async def deliver(self, request: DeliveryRequest) -> Optional[DeliveryReceipt]:
        if request.activation_id in self._delivered_activations:
            await asyncio.sleep(random.uniform(2.0, 5.0))
        ok = await self._send(request)
        if not ok:
            return None
        self._delivered_activations[request.activation_id] = None
        while len(self._delivered_activations) > self._SEEN_CAP:
            self._delivered_activations.pop(
                next(iter(self._delivered_activations))
            )
        return DeliveryReceipt()

    async def _send(self, request: DeliveryRequest) -> bool:
        raise NotImplementedError


class ChordialDeliverer(_PacedDeliverer):
    """adapts MessageRouter.deliver_as to the engine's Deliverer protocol."""

    def __init__(self, router):
        super().__init__()
        self.router = router

    async def _send(self, request: DeliveryRequest) -> bool:
        return await self.router.deliver_as(
            request.target.platform,
            request.target.target_id,
            request.text,
            speaker=request.speaker,
        )


# --- the delivery ledger ------------------------------------------------------
# BoundedDeliveryLedger was born here and upstreamed to the dainframe in
# phase 5 - chordial now uses the library class it proved out.


# --- assembly -----------------------------------------------------------------


def chordial_action_line(speaker: str, action: ExecutedAction) -> str:
    """the engine's action events render with chordial's frozen one-liner
    format - deterministic serialization, write-once, replayed verbatim."""
    return format_action_line(action.name, dict(action.input), action.result_content)


def build_orchestrator(
    *,
    agents: dict,
    user_manager: UserManager,
    agenda_service=None,
    reconciler=None,
    router=None,
    deliver=None,
    helper_state_manager: Optional[HelperStateManager] = None,
    message_window: Optional[int] = None,
) -> Orchestrator:
    """wire the dainframe engine with chordial's director, context, hooks,
    store, and deliverer. `router` is the production path (speaker-aware
    delivery + the switch notice); `deliver` is a raw (platform, target, text,
    speaker) -> bool awaitable for tests that don't want a router."""
    window = message_window or Config.MAX_HISTORY_MESSAGES
    deliver_fn = deliver if deliver is not None else (
        router.deliver_as if router is not None else None
    )
    return Orchestrator(
        agents=agents,
        store_factory=lambda sid: SqlEventStore(sid, visibility=chordial_visibility),
        director=ChordialDirector(
            agents.keys(), helper_state_manager=helper_state_manager
        ),
        context_provider=ChordialContext(user_manager, agenda_service),
        hooks=ChordialHooks(
            user_manager,
            reconciler=reconciler,
            deliver=deliver_fn,
            message_window=window,
        ),
        deliverer=(
            ChordialDeliverer(router) if router is not None
            else (_CallableDeliverer(deliver_fn) if deliver_fn else None)
        ),
        ledger=BoundedDeliveryLedger(),
        message_window=window,
        action_formatter=chordial_action_line,
    )


class _CallableDeliverer(_PacedDeliverer):
    """test convenience: wrap a bare (platform, target, text, speaker) -> bool
    awaitable as a Deliverer, with the same pacing as the router path."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    async def _send(self, request: DeliveryRequest) -> bool:
        return await self.fn(
            request.target.platform,
            request.target.target_id,
            request.text,
            speaker=request.speaker,
        )
