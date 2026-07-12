"""the orchestrator: chordial core.

decides WHO talks, assembles their briefing (the necessary info for this
activation), lets them act, and records what happened into the event log.
agents own how they think; this owns the conversation.

selection is `_direct`, the director: it produces a Script (a sequence of
ScriptLines - who speaks, in order). phase 2 is RULES ONLY (dm -> the dm'd
helper; group @mentions -> those helpers; else chordial); phase 3 makes the
group no-mention branch a cheap utility-model call. delivery is scope-aware:
production dm and group activations deliver through the speaker-aware router
and return handled=True only after a confirmed send. scheduled generation is
finalized by the scheduler after its delivery callback succeeds.

recording rules (per activation, in order):
  1. the inbound user message (for user_message stimuli) - written before the
     agent acts, so it's the last event in the briefing window
  2. the agent's successful mutating tool calls, as action events
  3. after confirmed platform delivery, the agent's reply as a message event
refusals and errors record NOTHING after the inbound message - a non-answer
never pollutes future context (long-standing invariant).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Dict, List, Optional, Tuple

from src.agents.base import Agent, AgentOutcome, Briefing
from src.managers.event_log import Event, EventLog
from src.managers.helper_state_manager import HelperStateManager
from src.managers.user_manager import UserManager
from src.providers.ai.types import ProviderError

# Stimulus/Deliverable (and the v3 Script/ScriptLine) live in orchestration_types
# so the platform adapter can construct them without importing the orchestrator.
# re-exported here so existing `from src.services.orchestrator import Stimulus`
# imports keep working.
from src.services.orchestration_types import (
    Deliverable,
    Script,
    ScriptLine,
    Stimulus,
)
from config import Config

logger = logging.getLogger(__name__)


# the one-time courtesy sent to a platform the conversation just walked away
# from. asterisk stage-direction styling matches the persona's voice; on
# plain-text platforms the asterisks read as deliberate emphasis.
SWITCH_NOTICE = "*(pssst — we're chatting over on {platform} now. see you there 💜)*"


class Orchestrator:
    def __init__(
        self,
        agents: Dict[str, Agent],
        user_manager: UserManager,
        agenda_service=None,
        tool_registry=None,
        reconciler=None,
        deliver=None,
        max_history_messages: Optional[int] = None,
        helper_state_manager: Optional[HelperStateManager] = None,
    ):
        self.agents = agents
        self.user_manager = user_manager
        # the director's cast source: which helpers this user has active. a
        # default is fine (it's stateless, one short session per read) so tests
        # and single-helper deployments need not wire one.
        self.helper_state_manager = helper_state_manager or HelperStateManager()
        self.agenda_service = agenda_service
        # the FULL registry (agents hold views of it) - consulted for the
        # record_event policy when persisting actions
        self.tool_registry = tool_registry
        # optional: after the companion replies to a user, a cheap pass that
        # marks tasks done which the user mentioned finishing in passing
        self.reconciler = reconciler
        # optional: speaker-aware awaitable
        # (platform, target_id, text, speaker) -> bool, wired to
        # MessageRouter.deliver_as in main. used for out-of-band sends: the
        # platform-switch courtesy and confirmed dm/group delivery (each line
        # spoken by the selected helper's own bot where supported).
        self.deliver = deliver
        self.max_history_messages = max_history_messages or Config.MAX_HISTORY_MESSAGES

    # --- entry point ---------------------------------------------------------

    async def handle(self, stimulus: Stimulus) -> Deliverable:
        log = EventLog(stimulus.user_uuid)
        scope, with_helper = self._scope_for(stimulus)

        # 1. record the inbound message first, so it's the last item in history.
        # (previous user message read BEFORE the append - it's what tells us
        # whether the conversation just walked to a different platform.) scope-
        # aware: a dm inbound stays in that helper's private channel.
        if stimulus.kind in ("user_message", "introduction") and stimulus.content:
            prev_user_event = (
                log.last_user_message() if stimulus.kind == "user_message" else None
            )
            await self._append_message(
                log,
                "user",
                "user",
                stimulus.content,
                stimulus=stimulus,
                scope=scope,
                with_helper=with_helper,
            )
            if stimulus.kind == "user_message":
                await self._maybe_notify_platform_switch(log, stimulus, prev_user_event)

        # 2. the director casts the script (rules only this phase).
        script = await self._direct(stimulus, log)

        deliverable = Deliverable()
        group = stimulus.chat_scope == "group"
        any_delivered = False

        # 3. run each line in order. a later speaker is briefed AFTER the
        # earlier line is recorded, so it genuinely reacts to what came before.
        for idx, line in enumerate(script.lines):
            agent = self.agents.get(line.speaker)
            if agent is None:
                logger.warning("orchestrator selected unknown agent '%s'", line.speaker)
                continue

            briefing = await self._brief(agent, stimulus, log, line)
            try:
                outcome = await agent.act(briefing)
            except ProviderError as e:
                logger.error("agent '%s' provider error: %s", agent.name, e)
                deliverable.errored = True
                continue

            if outcome.refused:
                deliverable.refused = True
            elif outcome.errored:
                deliverable.errored = True
            elif outcome.text:
                # Tool mutations have already happened, so keep their durable
                # action trail even when the reply itself cannot be delivered.
                await self._record_actions(log, agent, outcome, stimulus)
                if group:
                    # out-of-band: each bot speaks for itself in the shared
                    # channel. Only a confirmed send becomes conversation
                    # history; a natural gap between delivered lines lets it
                    # breathe.
                    delivered = False
                    if self.deliver is not None and stimulus.group_chat_id:
                        delivered = await self.deliver(
                            stimulus.platform,
                            stimulus.group_chat_id,
                            outcome.text,
                            speaker=line.speaker,
                        )
                    if delivered:
                        await self._record_message(log, agent, outcome.text, stimulus)
                        any_delivered = True
                    else:
                        logger.error(
                            "group reply from '%s' was not delivered; not recording it",
                            line.speaker,
                        )
                        deliverable.errored = True
                    if delivered and idx < len(script.lines) - 1:
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                elif stimulus.kind == "scheduled_tick":
                    # Generation and delivery are separate scheduler phases.
                    # Keep the text pending in memory only; the scheduler calls
                    # record_delivered_message after its callback returns True.
                    deliverable.text = outcome.text
                    deliverable.speaker = line.speaker
                else:
                    # Normal DMs and introductions can be sent here through the
                    # router, which gives us a real success signal before the
                    # message enters history. The no-hook branch is retained for
                    # isolated adapters/tests that still use synchronous return.
                    if self.deliver is not None and stimulus.delivery_target_id:
                        delivered = await self.deliver(
                            stimulus.platform,
                            stimulus.delivery_target_id,
                            outcome.text,
                            speaker=line.speaker,
                        )
                        if delivered:
                            await self._record_message(
                                log, agent, outcome.text, stimulus
                            )
                            deliverable.handled = True
                            any_delivered = True
                        else:
                            logger.error(
                                "dm reply from '%s' was not delivered; not recording it",
                                line.speaker,
                            )
                            deliverable.errored = True
                    else:
                        await self._record_message(log, agent, outcome.text, stimulus)
                        deliverable.text = outcome.text
                        deliverable.speaker = line.speaker
                        any_delivered = True

        # after a normal user turn, reconcile any tasks the user mentioned
        # finishing in passing (the companion's warmth can crowd out the
        # bookkeeping; this narrow pass catches what it missed)
        if stimulus.kind == "user_message" and any_delivered:
            await self._reconcile_completions(log, stimulus, scope, with_helper)

        # A group activation is handled only when at least one line actually
        # reached the room. If every send failed, the receiving interface may
        # still surface the ordinary error fallback.
        if group and any_delivered:
            deliverable.handled = True
        return deliverable

    async def curation_candidates(self) -> List[str]:
        """users whose memory tables have settled and want a curation pass."""
        curator = self.agents.get("curator")
        if curator is None:
            return []
        return await curator.find_users_needing_curation()

    # --- the director (phase 2: rules only; phase 3 adds the AI director) ------

    async def _direct(self, stimulus: Stimulus, log: EventLog) -> Script:
        """cast the script of speakers for this activation. deterministic this
        phase - phase 3 replaces the group specialty-match branch with a cheap
        utility-model call. the director must never break the conversation:
        every conversational path that empties falls back to chordial."""
        kind = stimulus.kind
        if kind == "curation_due":
            return Script([ScriptLine("curator")])
        if kind == "introduction":
            return self._finalize([ScriptLine(stimulus.intro_helper or "chordial")])
        if kind == "scheduled_tick":
            # the proactivity gate already cleared this tick upstream; phase 3's
            # AI director will choose venue/speaker.
            return self._finalize([ScriptLine("chordial")])
        if kind == "user_message":
            if stimulus.chat_scope != "group":
                # a dm is a private 1:1 - the addressed helper is the lone voice.
                return self._finalize([ScriptLine(stimulus.dm_helper or "chordial")])
            return self._finalize(await self._group_lines(stimulus))
        return Script([])  # unknown kind: cast nobody

    async def _group_lines(self, stimulus: Stimulus) -> List[ScriptLine]:
        """the group user_message routing rule. @-mentions win (in order,
        deduped, active cast only, capped at 2); otherwise chordial fields it.
        phase 3 replaces the no-mention branch with specialty-matching + an
        optional brief reactor."""
        if not stimulus.mentioned:
            return [ScriptLine("chordial")]
        cast = await self.helper_state_manager.active_helpers(stimulus.user_uuid)
        active_ids = {v.helper_id for v in cast if v.is_active}
        lines: List[ScriptLine] = []
        seen: set = set()
        for helper_id in stimulus.mentioned:
            if (
                helper_id in seen
                or helper_id not in active_ids
                or helper_id not in self.agents
            ):
                continue
            lines.append(ScriptLine(helper_id))
            seen.add(helper_id)
            if len(lines) >= 2:
                break
        # every mention was inactive/unknown - don't drop the message on the floor
        return lines or [ScriptLine("chordial")]

    def _finalize(self, lines: List[ScriptLine]) -> Script:
        """the director's hard guardrail: cap at 2 lines, drop any speaker
        without an agent, and never return empty (fall back to chordial)."""
        kept = [line for line in lines[:2] if line.speaker in self.agents]
        return Script(kept or [ScriptLine("chordial")])

    @staticmethod
    def _scope_for(stimulus: Stimulus) -> Tuple[str, Optional[str]]:
        """(scope, with_helper) for recording this activation's events. group
        writes no scope tag (absence means group); a dm is tagged to its helper
        - the resolved speaker, so the legacy single-helper dm (dm_helper=None)
        stays visible to chordial's own privacy-scoped window."""
        if stimulus.chat_scope == "group":
            return "group", None
        if stimulus.kind == "introduction":
            return "dm", (stimulus.intro_helper or "chordial")
        return "dm", (stimulus.dm_helper or "chordial")

    # --- briefing assembly -----------------------------------------------------

    async def _brief(
        self,
        agent: Agent,
        stimulus: Stimulus,
        log: EventLog,
        line: Optional[ScriptLine] = None,
    ) -> Briefing:
        # curation needs no conversation context - keep it light
        if stimulus.kind == "curation_due":
            return Briefing(
                kind="curation",
                user_uuid=stimulus.user_uuid,
                platform=stimulus.platform,
            )

        user_name, user_timezone = stimulus.user_name, stimulus.user_timezone
        if user_timezone is None:
            # caller didn't resolve the profile (e.g. the scheduler) - one query
            user_name, user_timezone = await self.user_manager.get_user_profile(
                stimulus.user_uuid
            )

        if stimulus.kind == "introduction":
            briefing_kind = "introduction"
        elif stimulus.kind == "scheduled_tick":
            briefing_kind = "scheduled_checkin"
        else:
            briefing_kind = "user_message"

        return Briefing(
            kind=briefing_kind,
            user_uuid=stimulus.user_uuid,
            platform=stimulus.platform,
            user_name=user_name,
            user_timezone=user_timezone or "UTC",
            # privacy-scoped window: this helper sees the group channel plus its
            # OWN dms, never a sibling's private transcript.
            events=self._visible_window(log, agent.name),
            # an introduction stays light (no agenda digest) but keeps the event
            # window, so a returning user's intro sees prior context.
            ambient_context=(
                None
                if briefing_kind == "introduction"
                else self._compose_ambient(stimulus.user_uuid)
            ),
            cue=line.cue if line else None,
            style=line.style if line else "full",
            scope=stimulus.chat_scope,
        )

    def _visible_window(self, log: EventLog, helper_id: str) -> List[Event]:
        """the privacy-scoped event window for `helper_id`: the shared group
        channel plus its OWN dms, never a sibling's private transcript. the
        event log applies the predicate and windows on the visible messages."""
        return log.recent(self.max_history_messages, visible_to=helper_id)

    def _compose_ambient(self, user_uuid: str) -> Optional[str]:
        """the ambient context for the volatile 'now' turn (notion agenda
        digest; morning note joins it when daily passes land). pure db reads,
        fully guarded - any failure degrades to None, i.e. today's exact
        prompt bytes."""
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

    # --- recording ---------------------------------------------------------------

    async def _record_actions(
        self, log: EventLog, agent: Agent, outcome: AgentOutcome, stimulus: Stimulus
    ) -> None:
        """Persist successful mutation actions independently of delivery.

        The side effects already happened inside the agent loop, so their event
        trail remains true even if the platform cannot carry the prose reply.
        """
        scope, with_helper = self._scope_for(stimulus)
        for action in outcome.actions:
            if action.is_error:
                continue
            if self.tool_registry and not self.tool_registry.should_record(action.name):
                continue
            log.append_action(
                agent.name,
                action.name,
                action.input,
                action.result_content,
                platform=stimulus.platform,
                scope=scope,
                with_helper=with_helper,
            )

    async def _record_message(
        self, log: EventLog, agent: Agent, text: str, stimulus: Stimulus
    ) -> None:
        """Commit a reply only after its delivery path has succeeded."""
        scope, with_helper = self._scope_for(stimulus)
        message_type = (
            "scheduled" if stimulus.kind == "scheduled_tick" else "conversation"
        )
        await self._append_message(
            log,
            "agent",
            agent.name,
            text,
            stimulus=stimulus,
            message_type=message_type,
            scope=scope,
            with_helper=with_helper,
        )

    async def record_delivered_message(
        self,
        *,
        user_uuid: str,
        platform: str,
        speaker: str,
        text: str,
        message_type: str = "scheduled",
    ) -> None:
        """Finalize an outbound line whose caller confirmed platform delivery.

        Scheduled generation deliberately returns before sending, so the
        scheduler uses this small boundary after its delivery callback succeeds.
        """
        log = EventLog(user_uuid)
        stimulus = Stimulus(
            kind="scheduled_tick" if message_type == "scheduled" else "user_message",
            user_uuid=user_uuid,
            platform=platform,
            chat_scope="dm",
            dm_helper=speaker,
        )
        agent = self.agents.get(speaker)
        if agent is None:
            logger.error(
                "cannot record delivered message for unknown agent '%s'", speaker
            )
            return
        await self._record_message(log, agent, text, stimulus)

    async def _reconcile_completions(
        self,
        log: EventLog,
        stimulus: Stimulus,
        scope: str = "group",
        with_helper: Optional[str] = None,
    ) -> None:
        """run the completion reconciler and record any Done marks it made as
        the companion's own actions (so the replay reads coherently and the
        companion sees next turn what it 'noticed'). fully guarded - a failure
        here must never affect the reply the user already got. the actions land
        in the same scope as the turn that triggered them."""
        if self.reconciler is None or not stimulus.content:
            return
        try:
            recent = log.recent(self.max_history_messages)
            # drop the just-recorded inbound message; it's passed separately
            recent = [
                e
                for e in recent
                if not (
                    e.kind == "message"
                    and e.role == "user"
                    and e.content == stimulus.content
                )
            ][-6:]
            result = await self.reconciler.reconcile(
                user_uuid=stimulus.user_uuid,
                platform=stimulus.platform,
                message_text=stimulus.content,
                recent=recent,
            )
            for action in result.actions:
                if not action.is_error:
                    log.append_action(
                        "chordial",
                        action.name,
                        action.input,
                        action.result_content,
                        platform=stimulus.platform,
                        scope=scope,
                        with_helper=with_helper,
                    )
        except Exception as e:
            logger.error(
                "completion reconcile failed for user %s: %s", stimulus.user_uuid, e
            )

    async def _maybe_notify_platform_switch(
        self, log: EventLog, stimulus: Stimulus, prev_user_event
    ) -> None:
        """the conversation just walked from platform A to platform B: send the
        one-time courtesy notice to A. structurally self-deduping - after this
        message, the last user message IS on B, so the trigger can't refire
        until the user speaks on A again (which is the reset the owner asked
        for). fully guarded: a failure here must never affect the user's reply.
        """
        try:
            if self.deliver is None or prev_user_event is None:
                return
            old_platform = prev_user_event.platform
            if not old_platform or old_platform == stimulus.platform:
                return

            # only notify a link we could actually reach
            identity = await self.user_manager.get_identity(
                stimulus.user_uuid, old_platform
            )
            if identity is None or not identity[1]:
                return
            platform_user_id = identity[0]

            # belt-and-braces for two rapid messages dispatched concurrently:
            # if a switch note for A was already recorded after A's last user
            # message, another handler beat us to it. this scan sits directly
            # before the note append with NO await between them, so the
            # check-then-write pair is atomic within the event loop.
            for event in reversed(log.recent(self.max_history_messages)):
                if (
                    event.db_id
                    and prev_user_event.db_id
                    and event.db_id <= prev_user_event.db_id
                ):
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
            log.append_note(
                notice,
                platform=old_platform,
                metadata={"note_type": "platform_switch", "to": stimulus.platform},
            )
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
                "platform-switch notice failed for user %s: %s", stimulus.user_uuid, e
            )

    async def _append_message(
        self,
        log: EventLog,
        author_type: str,
        author: str,
        content: str,
        *,
        stimulus: Stimulus,
        message_type: str = "conversation",
        scope: str = "group",
        with_helper: Optional[str] = None,
    ) -> None:
        event = log.append_message(
            author_type,
            author,
            content,
            message_type=message_type,
            platform=stimulus.platform,
            scope=scope,
            with_helper=with_helper,
        )
        if Config.ENABLE_COMPRESSION:
            await self._compress(event, stimulus)

    async def _compress(self, event: Event, stimulus: Stimulus) -> None:
        """legacy per-message compression hook (ENABLE_COMPRESSION, off by
        default): store a compressed twin of a just-logged message event."""
        try:
            from src.services.compressor_service import CompressorService

            if event.db_id is None:
                logger.warning("event has no db_id, skipping compression")
                return
            compressor = CompressorService()
            compressed = await compressor.compress_message(event.content, event.role)
            await compressor.store_compressed_message(
                conversation_history_id=event.db_id,
                user_uuid=stimulus.user_uuid,
                platform=stimulus.platform,
                role=event.role,
                original_content=event.content,
                compressed_content=compressed,
            )
        except Exception as e:
            logger.error(f"compression failed (continuing uncompressed): {e}")
