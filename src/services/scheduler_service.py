from datetime import datetime, timedelta
from typing import Dict, Optional, List
import asyncio
import logging

from src.managers.user_manager import UserManager
from src.managers.event_log import EventLog
from src.services.orchestrator import Stimulus
from src.utils.timezone_utils import utc_now, get_user_local_hour, is_within_quiet_hours
from config import Config

logger = logging.getLogger(__name__)

class ScheduledMessageContext:
    """tracks scheduling context for each user"""
    def __init__(self, user_uuid: str, platform: str):
        self.user_uuid = user_uuid
        self.platform = platform
        self.last_scheduled_at: Optional[datetime] = None
        self.last_message_was_scheduled: bool = False
        self.next_scheduled_time: Optional[datetime] = None

class SchedulerService:
    """handles intelligent scheduling of messages across all platforms"""
    
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
        self.user_contexts: Dict[str, ScheduledMessageContext] = {}
        self.default_interval_minutes = Config.DM_INTERVAL_MINUTES
        self.delay_after_ignored_hours = Config.DELAY_AFTER_IGNORED_HOURS  # delay N hours if scheduled message was ignored
        self.quiet_hours_start = Config.QUIET_HOURS_START
        self.quiet_hours_end = Config.QUIET_HOURS_END 
        self._running = False
    
    def _get_context_key(self, user_uuid: str, platform: str) -> str:
        """generate unique key for user context"""
        return f"{platform}:{user_uuid}"
    
    def _get_or_create_context(self, user_uuid: str, platform: str) -> ScheduledMessageContext:
        """get or create scheduling context for a user"""
        key = self._get_context_key(user_uuid, platform)
        if key not in self.user_contexts:
            self.user_contexts[key] = ScheduledMessageContext(user_uuid, platform)
        return self.user_contexts[key]
    
    async def _check_last_message(self, user_uuid: str, platform: str) -> tuple[Optional[str], Optional[datetime], Optional[str]]:
        """check who sent the last message, when, and what type. reads the
        event log's last MESSAGE event - tool-action events are invisible here
        by construction, so a trailing action can never masquerade as 'the
        assistant just replied'."""
        event = EventLog(user_uuid, platform).last_message()
        if event:
            return event.role, event.created_at, event.message_type
        return None, None, None
    
    def _is_quiet_hours(self, user_timezone: str) -> bool:
        """check if it's currently quiet hours (default: after 9pm or before 8am) in the user's local time"""
        local_hour = get_user_local_hour(utc_now(), user_timezone)
        return is_within_quiet_hours(local_hour, self.quiet_hours_start, self.quiet_hours_end)

    async def should_send_scheduled_message(self, user_uuid: str, platform: str) -> bool:
        """determine if we should send a scheduled message to this user now"""
        context = self._get_or_create_context(user_uuid, platform)
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
        last_role, last_message_time, last_message_type = await self._check_last_message(user_uuid, platform)
        
        # if no messages yet, send one
        if not last_role:
            logger.info(f"no messages found for user {user_uuid}, sending first scheduled message")
            return True
        
        # calculate time since last message
        time_since_last = now - last_message_time if last_message_time else timedelta(hours=999)
        
        # if last message was a scheduled message (ignored by user)
        if last_role == "assistant" and last_message_type == "scheduled":
            # check if it's been 24 hours
            if time_since_last < timedelta(hours=self.delay_after_ignored_hours):
                logger.debug(f"last message was scheduled and sent {time_since_last} ago, waiting 24h")
                return False
            else:
                logger.info(f"24h passed since ignored scheduled message for user {user_uuid}")
                return True
        
        # last message was from user or was a conversation response
        else:
            # check if we're in quiet hours (in the user's own timezone)
            user_timezone = await self.user_manager.get_user_timezone(user_uuid)
            if self._is_quiet_hours(user_timezone):
                logger.debug(f"in quiet hours for user {user_uuid} (tz={user_timezone}), not sending scheduled message")
                return False
            
            # check if enough time has passed for regular interval
            if time_since_last >= timedelta(minutes=self.default_interval_minutes):
                logger.info(f"regular interval passed for active user {user_uuid}")
                return True
            else:
                logger.debug(f"only {time_since_last} since last user message, waiting")
                return False
    
    async def send_scheduled_message(self, user_uuid: str, platform: str, platform_user_id: str) -> Optional[str]:
        """send a scheduled message if appropriate"""
        if self.orchestrator is None:
            return None
        if not await self.should_send_scheduled_message(user_uuid, platform):
            return None

        deliverable = await self.orchestrator.handle(Stimulus(
            kind="scheduled_tick", user_uuid=user_uuid, platform=platform,
        ))
        message = deliverable.text if not (deliverable.refused or deliverable.errored) else None

        if message:
            context = self._get_or_create_context(user_uuid, platform)
            context.last_scheduled_at = utc_now()
            logger.info(f"generated scheduled message for user {user_uuid} (platform: {platform_user_id}): {message[:50]}...")

        return message
    
    async def run_scheduling_loop(self, platforms: List[str], message_callback):
        """main scheduling loop that checks all users across platforms"""
        self._running = True
        check_interval = 60*5  # check every 5 minutes
        
        while self._running:
            try:
                for platform in platforms:
                    user_mappings = await self.user_manager.get_users_with_scheduled_messages(platform)

                    for user_uuid, platform_user_id in user_mappings:
                        # keep the agenda snapshot warm before we (maybe) message
                        await self._refresh_agenda(user_uuid)
                        message = await self.send_scheduled_message(user_uuid, platform, platform_user_id)
                        if message:
                            # use the callback to actually send the message
                            await message_callback(platform, platform_user_id, message)

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