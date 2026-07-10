from datetime import datetime, timedelta
from typing import Optional, List
import asyncio
import logging

from src.managers.user_manager import UserManager
from src.managers.event_log import EventLog
from src.services.orchestrator import Stimulus
from src.services.proactivity_gate import ProactivityGate
from src.utils.timezone_utils import utc_now, get_user_local_hour, is_within_quiet_hours
from config import Config

logger = logging.getLogger(__name__)


class SchedulerService:
    """handles intelligent scheduling of messages across all platforms.

    scheduling is per-USER (one conversation, one recency clock, however many
    platforms they're on); delivery targets the platform the user most
    recently spoke on, falling back to any other live link."""

    def __init__(self, orchestrator=None, user_manager: UserManager = None,
                 agenda_service=None):
        # the orchestrator generates scheduled check-ins and runs the curation
        # pass; None (e.g. no ai provider configured) disables both quietly
        self.orchestrator = orchestrator
        self.user_manager = user_manager or UserManager()
        # optional: keeps each user's notion agenda snapshot fresh (the chat
        # path only ever reads it, so this background refresh is where notion
        # actually gets queried)
        self.agenda_service = agenda_service
        self.default_interval_minutes = Config.DM_INTERVAL_MINUTES
        self.quiet_hours_start = Config.QUIET_HOURS_START
        self.quiet_hours_end = Config.QUIET_HOURS_END
        # the non-interaction guard: pure db arithmetic, checked before any
        # generation. phase 1 has one helper ('chordial'); a later phase
        # passes real per-helper candidates through here.
        self.gate = ProactivityGate()
        self._running = False

    async def _check_last_message(self, user_uuid: str) -> tuple[Optional[str], Optional[datetime], Optional[str]]:
        """check who sent the last message, when, and what type. reads the
        event log's last MESSAGE event - tool-action and note events are
        invisible here by construction, so a trailing action or switch notice
        can never masquerade as 'the assistant just replied'."""
        event = EventLog(user_uuid).last_message()
        if event:
            return event.role, event.created_at, event.message_type
        return None, None, None
    
    def _is_quiet_hours(self, user_timezone: str) -> bool:
        """check if it's currently quiet hours (default: after 9pm or before 8am) in the user's local time"""
        local_hour = get_user_local_hour(utc_now(), user_timezone)
        return is_within_quiet_hours(local_hour, self.quiet_hours_start, self.quiet_hours_end)

    async def should_send_scheduled_message(self, user_uuid: str) -> bool:
        """determine if we should send a scheduled message to this user now.
        the gate runs against the user's UNIFIED conversation - chatting on
        any platform resets the recency clock for all of them.

        order: onboarding -> first contact -> quiet hours -> the
        proactivity gate (the ignored-chain guard) -> regular interval. quiet
        hours applies to every proactive send, including the ignored chain -
        being ignored is not license to nag at 3am."""
        now = utc_now()

        # check if user has completed onboarding. LOAD-BEARING for the
        # orchestrator path: send_scheduled_message goes straight to the
        # orchestrator (no onboarding pre-check of its own), so this gate is
        # the only thing keeping scheduled sends away from mid-onboarding users.
        needs_onboarding = await self.user_manager.needs_onboarding(user_uuid)
        if needs_onboarding:
            logger.debug(f"user {user_uuid} needs onboarding, skipping scheduled message")
            return False

        # check last message in conversation
        last_role, last_message_time, last_message_type = await self._check_last_message(user_uuid)

        # if no messages yet, send one
        if not last_role:
            logger.info(f"no messages found for user {user_uuid}, sending first scheduled message")
            return True

        # check if we're in quiet hours (in the user's own timezone) - applies
        # to every proactive send, ignored-chain or fresh
        user_timezone = await self.user_manager.get_user_timezone(user_uuid)
        if self._is_quiet_hours(user_timezone):
            logger.debug(f"in quiet hours for user {user_uuid} (tz={user_timezone}), not sending scheduled message")
            return False

        # the non-interaction guard: crew cap, per-helper cap, exponential
        # backoff - pure db arithmetic, no tokens spent on a denied tick
        decision = self.gate.check(EventLog(user_uuid), "chordial")
        if not decision.allowed:
            logger.debug(f"proactivity gate denied user {user_uuid}: {decision.reason}")
            return False

        # gate is clear. if there's an active ignored chain (unanswered > 0)
        # the gate's own backoff already governs timing - send now. otherwise
        # this is fresh outreach after a real reply, so the regular interval
        # still applies.
        if last_role == "assistant" and last_message_type == "scheduled":
            logger.info(f"proactivity gate clear for ignored chain, user {user_uuid}")
            return True

        time_since_last = now - last_message_time if last_message_time else timedelta(hours=999)
        if time_since_last >= timedelta(minutes=self.default_interval_minutes):
            logger.info(f"regular interval passed for active user {user_uuid}")
            return True
        else:
            logger.debug(f"only {time_since_last} since last user message, waiting")
            return False
    
    async def send_scheduled_message(
        self, user_uuid: str, platforms: Optional[List[str]] = None,
    ) -> Optional[tuple[str, str, str]]:
        """generate a scheduled message if appropriate, targeted at the
        platform the user most recently spoke on (falling back to any other
        live link). returns (platform, platform_user_id, message) or None.
        the target is resolved BEFORE the orchestrator runs, so no tokens are
        spent when there's nowhere to deliver, and the reply event's
        provenance is the real destination."""
        if self.orchestrator is None:
            return None
        if not await self.should_send_scheduled_message(user_uuid):
            return None

        active = EventLog(user_uuid).active_platform()
        target = await self.user_manager.resolve_delivery_identity(
            user_uuid, active, platforms,
        )
        if target is None:
            logger.debug(f"no deliverable platform for user {user_uuid}, skipping")
            return None
        platform, platform_user_id = target

        deliverable = await self.orchestrator.handle(Stimulus(
            kind="scheduled_tick", user_uuid=user_uuid, platform=platform,
        ))
        message = deliverable.text if not (deliverable.refused or deliverable.errored) else None

        if message:
            logger.info(f"generated scheduled message for user {user_uuid} -> {platform}: {message[:50]}...")
            return platform, platform_user_id, message
        return None

    async def run_scheduling_loop(self, platforms: List[str], message_callback):
        """main scheduling loop: one pass per USER per cycle (a person on two
        platforms is one schedule slot, not two). `platforms` is the set with
        a live interface - delivery targeting is restricted to it."""
        self._running = True
        check_interval = 60*5  # check every 5 minutes

        while self._running:
            try:
                for user_uuid in await self.user_manager.get_scheduled_users():
                    # keep the agenda snapshot warm before we (maybe) message
                    await self._refresh_agenda(user_uuid)
                    result = await self.send_scheduled_message(user_uuid, platforms)
                    if result:
                        # use the callback to actually send the message
                        await message_callback(*result)

                # piggyback the memory-cleanup pass on the same cycle
                await self._run_curation_pass()

            except Exception as e:
                logger.error(f"error in scheduling loop: {e}")

            await asyncio.sleep(check_interval)

    async def _refresh_agenda(self, user_uuid: str) -> None:
        """refresh this user's notion agenda snapshot if it's stale/expired -
        but only outside their quiet hours. nobody's chatting overnight, so
        there's no reason to spend notion calls keeping it warm then; it
        catches back up within one 5-min cycle of quiet hours ending, well
        before a human is likely to say good morning.
        guarded - notion being slow or down must never stall message delivery."""
        if not self.agenda_service:
            return
        try:
            user_timezone = await self.user_manager.get_user_timezone(user_uuid)
            if self._is_quiet_hours(user_timezone):
                return
            await self.agenda_service.ensure_fresh(user_uuid)
        except Exception as e:
            logger.error(f"agenda refresh failed for user {user_uuid}: {e}")

    async def _run_curation_pass(self) -> None:
        """let the curator tidy any user whose new memories have settled. kept
        defensive - a curation failure must never stall message scheduling."""
        if self.orchestrator is None:
            return
        try:
            user_uuids = await self.orchestrator.curation_candidates()
            for user_uuid in user_uuids:
                await self.orchestrator.handle(Stimulus(kind="curation_due", user_uuid=user_uuid))
        except Exception as e:
            logger.error(f"error in memory curation pass: {e}")
    
    def stop(self):
        """stop the scheduling loop"""
        self._running = False