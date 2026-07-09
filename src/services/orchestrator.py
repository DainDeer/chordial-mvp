"""the orchestrator: chordial core.

decides WHO talks, assembles their briefing (the necessary info for this
activation), lets them act, and records what happened into the event log.
agents own how they think; this owns the conversation.

v2 selection is deterministic (a static stimulus->agent map). v3's director -
ai-driven selection producing a scripted sequence of speakers - replaces
_select/_brief without touching any agent or the recording rules.

recording rules (per activation, in order):
  1. the inbound user message (for user_message stimuli) - written before the
     agent acts, so it's the last event in the briefing window
  2. the agent's successful mutating tool calls, as action events
  3. the agent's reply, as a message event
refusals and errors record NOTHING after the inbound message - a non-answer
never pollutes future context (long-standing invariant).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.agents.base import Agent, AgentOutcome, Briefing
from src.managers.event_log import Event, EventLog
from src.managers.user_manager import UserManager
from src.providers.ai.types import ProviderError
from config import Config

logger = logging.getLogger(__name__)


@dataclass
class Stimulus:
    """something that might make an agent act."""
    kind: str                        # 'user_message' | 'scheduled_tick' | 'curation_due'
    user_uuid: str
    platform: Optional[str] = None
    content: Optional[str] = None    # user_message only
    user_name: Optional[str] = None
    user_timezone: Optional[str] = None


@dataclass
class Deliverable:
    """what the caller (platform adapter / scheduler) gets back."""
    text: Optional[str] = None
    refused: bool = False
    errored: bool = False


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
    ):
        self.agents = agents
        self.user_manager = user_manager
        self.agenda_service = agenda_service
        # the FULL registry (agents hold views of it) - consulted for the
        # record_event policy when persisting actions
        self.tool_registry = tool_registry
        # optional: after the companion replies to a user, a cheap pass that
        # marks tasks done which the user mentioned finishing in passing
        self.reconciler = reconciler
        # optional: awaitable (platform, platform_user_id, message) -> bool,
        # wired to MessageRouter.deliver in main - lets the orchestrator send
        # out-of-band courtesies like the platform-switch notice
        self.deliver = deliver
        self.max_history_messages = max_history_messages or Config.MAX_HISTORY_MESSAGES

    # --- entry point ---------------------------------------------------------

    async def handle(self, stimulus: Stimulus) -> Deliverable:
        log = EventLog(stimulus.user_uuid)

        # 1. record the inbound message first, so it's the last item in history.
        # (previous user message read BEFORE the append - it's what tells us
        # whether the conversation just walked to a different platform.)
        if stimulus.kind == "user_message" and stimulus.content:
            prev_user_event = log.last_user_message()
            await self._append_message(log, "user", "user", stimulus.content, stimulus=stimulus)
            await self._maybe_notify_platform_switch(log, stimulus, prev_user_event)

        deliverable = Deliverable()
        for speaker in self._select(stimulus):
            agent = self.agents.get(speaker)
            if agent is None:
                logger.warning("orchestrator selected unknown agent '%s'", speaker)
                continue

            briefing = await self._brief(agent, stimulus, log)
            try:
                outcome = await agent.act(briefing)
            except ProviderError as e:
                logger.error("agent '%s' provider error: %s", agent.name, e)
                deliverable.errored = True
                continue

            await self._record(log, agent, outcome, stimulus)

            if outcome.refused:
                deliverable.refused = True
            elif outcome.errored:
                deliverable.errored = True
            elif outcome.text:
                # v2: single speaker, single deliverable. (v3: a script of
                # sequential deliveries lands here.)
                deliverable.text = outcome.text

        # after a normal user turn, reconcile any tasks the user mentioned
        # finishing in passing (the companion's warmth can crowd out the
        # bookkeeping; this narrow pass catches what it missed)
        if stimulus.kind == "user_message" and deliverable.text:
            await self._reconcile_completions(log, stimulus)

        return deliverable

    async def curation_candidates(self) -> List[str]:
        """users whose memory tables have settled and want a curation pass."""
        curator = self.agents.get("curator")
        if curator is None:
            return []
        return await curator.find_users_needing_curation()

    # --- selection (v2: static; v3: the director) ------------------------------

    def _select(self, stimulus: Stimulus) -> List[str]:
        return {
            "user_message": ["chordial"],
            "scheduled_tick": ["chordial"],
            "curation_due": ["curator"],
        }.get(stimulus.kind, [])

    # --- briefing assembly -----------------------------------------------------

    async def _brief(self, agent: Agent, stimulus: Stimulus, log: EventLog) -> Briefing:
        # curation needs no conversation context - keep it light
        if stimulus.kind == "curation_due":
            return Briefing(kind="curation", user_uuid=stimulus.user_uuid,
                            platform=stimulus.platform)

        user_name, user_timezone = stimulus.user_name, stimulus.user_timezone
        if user_timezone is None:
            # caller didn't resolve the profile (e.g. the scheduler) - one query
            user_name, user_timezone = await self.user_manager.get_user_profile(stimulus.user_uuid)

        return Briefing(
            kind="scheduled_checkin" if stimulus.kind == "scheduled_tick" else "user_message",
            user_uuid=stimulus.user_uuid,
            platform=stimulus.platform,
            user_name=user_name,
            user_timezone=user_timezone or "UTC",
            events=log.recent(self.max_history_messages),
            ambient_context=self._compose_ambient(stimulus.user_uuid),
        )

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

    async def _record(self, log: EventLog, agent: Agent, outcome: AgentOutcome,
                      stimulus: Stimulus) -> None:
        """persist an activation's outcome: actions first (chronology), then
        the reply. refused/errored/silent outcomes record nothing."""
        if outcome.refused or outcome.errored or not outcome.text:
            return

        for action in outcome.actions:
            if action.is_error:
                continue
            if self.tool_registry and not self.tool_registry.should_record(action.name):
                continue
            log.append_action(agent.name, action.name, action.input, action.result_content,
                              platform=stimulus.platform)

        message_type = "scheduled" if stimulus.kind == "scheduled_tick" else "conversation"
        await self._append_message(log, "agent", agent.name, outcome.text,
                                   stimulus=stimulus, message_type=message_type)

    async def _reconcile_completions(self, log: EventLog, stimulus: Stimulus) -> None:
        """run the completion reconciler and record any Done marks it made as
        the companion's own actions (so the replay reads coherently and the
        companion sees next turn what it 'noticed'). fully guarded - a failure
        here must never affect the reply the user already got."""
        if self.reconciler is None or not stimulus.content:
            return
        try:
            recent = log.recent(self.max_history_messages)
            # drop the just-recorded inbound message; it's passed separately
            recent = [e for e in recent if not (e.kind == "message" and e.role == "user"
                      and e.content == stimulus.content)][-6:]
            result = await self.reconciler.reconcile(
                user_uuid=stimulus.user_uuid,
                platform=stimulus.platform,
                message_text=stimulus.content,
                recent=recent,
            )
            for action in result.actions:
                if not action.is_error:
                    log.append_action("chordial", action.name, action.input,
                                      action.result_content, platform=stimulus.platform)
        except Exception as e:
            logger.error("completion reconcile failed for user %s: %s",
                         stimulus.user_uuid, e)

    async def _maybe_notify_platform_switch(self, log: EventLog, stimulus: Stimulus,
                                            prev_user_event) -> None:
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
            identity = await self.user_manager.get_identity(stimulus.user_uuid, old_platform)
            if identity is None or not identity[1]:
                return
            platform_user_id = identity[0]

            # belt-and-braces for two rapid messages dispatched concurrently:
            # if a switch note for A was already recorded after A's last user
            # message, another handler beat us to it. this scan sits directly
            # before the note append with NO await between them, so the
            # check-then-write pair is atomic within the event loop.
            for event in reversed(log.recent(self.max_history_messages)):
                if event.db_id and prev_user_event.db_id and event.db_id <= prev_user_event.db_id:
                    break
                if (event.kind == "note"
                        and event.metadata.get("note_type") == "platform_switch"
                        and event.platform == old_platform):
                    return

            notice = SWITCH_NOTICE.format(platform=stimulus.platform)
            # note first (the at-most-once record), then best-effort delivery -
            # a transient miss is an acceptable cost for a cosmetic courtesy
            log.append_note(notice, platform=old_platform,
                            metadata={"note_type": "platform_switch", "to": stimulus.platform})
            delivered = await self.deliver(old_platform, platform_user_id, notice)
            if not delivered:
                logger.info("switch notice to %s not delivered (transient or dead link)",
                            old_platform)
        except Exception as e:
            logger.error("platform-switch notice failed for user %s: %s",
                         stimulus.user_uuid, e)

    async def _append_message(self, log: EventLog, author_type: str, author: str,
                              content: str, *, stimulus: Stimulus,
                              message_type: str = "conversation") -> None:
        event = log.append_message(author_type, author, content,
                                   message_type=message_type,
                                   platform=stimulus.platform)
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
