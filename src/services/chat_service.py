from typing import Optional
import logging

from src.models.unified_message import UnifiedMessage
from src.managers.event_log import EventLog, Event
from src.managers.user_manager import UserManager
from src.services.onboarding_service import OnboardingService
from src.services.prompt_service import PromptService
from src.providers.ai.types import ProviderError
from config import Config

logger = logging.getLogger(__name__)

# in-character copy for the two non-response outcomes. never persisted to
# history (so they can't pollute future context).
REFUSAL_REPLY = "i don't think i can help with that one, but i'm here for whatever else is on your mind 💛"
ERROR_REPLY = "i'm having a little trouble reaching my thoughts right now — mind trying again in a bit?"

# the event-log author id for the (currently one and only) chat persona.
# becomes Agent.name when the orchestrator/agent split lands.
AGENT_AUTHOR = "chordial"


class ChatService:
    """main service for handling chat interactions across all platforms"""

    def __init__(
        self,
        agent_service=None,
        user_manager=None,
        tool_registry=None,
        max_history_messages: int = None,
        agenda_service=None,
        daily_pass_service=None,
    ):
        self.agent_service = agent_service
        self.user_manager = user_manager or UserManager()
        self.onboarding_service = OnboardingService(self.user_manager)
        self.prompt_service = PromptService()
        self.tool_registry = tool_registry
        self.max_history_messages = max_history_messages or Config.MAX_HISTORY_MESSAGES
        # optional proactive-notion services: agenda snapshot (ambient digest)
        # and daily passes (morning context note). both read-only here, and both
        # strictly db reads - the chat path never waits on notion.
        self.agenda_service = agenda_service
        self.daily_pass_service = daily_pass_service

    def _tool_defs(self):
        return self.tool_registry.definitions() if self.tool_registry else []

    def _compose_ambient(self, user_uuid: str, user_timezone: str) -> Optional[str]:
        """assemble the ambient context injected into the volatile 'now' turn:
        the notion agenda digest (+ the morning note once daily passes land).
        pure db reads, fully guarded - any failure degrades to no ambient
        context, i.e. exactly today's prompt bytes."""
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

    def _record_actions(self, log: EventLog, result) -> None:
        """persist the turn's executed tool calls as action events, so future
        turns can see what was already done (the cross-turn dedup fix). policy:
        only successful calls, only tools flagged record_event (mutations, not
        pure reads). recorded BEFORE the reply message event - chronology."""
        if not self.tool_registry:
            return
        for action in result.actions:
            if action.is_error or not self.tool_registry.should_record(action.name):
                continue
            log.append_action(AGENT_AUTHOR, action.name, action.input, action.result_content)

    async def _prepare_for_interaction(
        self,
        platform: str,
        platform_user_id: str,
        platform_user_name: Optional[str] = None,
        content: Optional[str] = None
    ) -> tuple[str, Optional[str], bool, Optional[str], str]:
        """
        helper function to handle user creation and onboarding flow.

        returns a tuple containing:
        - the user uuid (string)
        - the user's preferred name (string)
        - whether the interaction should continue (True) or stop due to onboarding (False)
        - an optional response message (e.g., welcome message or onboarding response)
        - the user's timezone (string), resolved once here so downstream
          callers (event log, prompt service) don't each re-fetch it from the
          database for the same interaction
        """
        # check if this is a brand new user
        is_new = await self.user_manager.is_new_user(platform, platform_user_id)

        # get or create user (returns user id string and preferred name if it exists)
        user_uuid, user_name = await self.user_manager.get_or_create_user(
            platform,
            platform_user_id,
            platform_user_name
        )

        # resolve once per interaction; passed down explicitly from here on
        user_timezone = await self.user_manager.get_user_timezone(user_uuid)

        # handle brand new users
        if is_new:
            self.onboarding_service.start_onboarding(platform, platform_user_id)
            return user_uuid, None, False, self.onboarding_service.get_welcome_message(), user_timezone

        # check if user needs onboarding (no preferred name set)
        needs_onboarding = user_name is None

        # check if user is in onboarding flow
        if self.onboarding_service.is_user_onboarding(platform, platform_user_id) or needs_onboarding:
            if content is not None:
                user_name, response = await self.onboarding_service.handle_onboarding_response(
                    user_uuid,
                    platform,
                    platform_user_id,
                    content
                )
                return user_uuid, user_name, False, response, user_timezone
            else:
                logger.info(f"skipping interaction for {platform_user_name} - in onboarding but no content to process")
                return user_uuid, None, False, None, user_timezone

        # user is fully onboarded, continue with normal flow
        return user_uuid, user_name, True, None, user_timezone

    async def process_message(self, unified_message: UnifiedMessage) -> Optional[str]:
        """process an incoming message and generate a response"""
        try:
            user_uuid, user_name, should_continue, response, user_timezone = await self._prepare_for_interaction(
                platform=unified_message.platform,
                platform_user_id=unified_message.platform_user_id,
                platform_user_name=unified_message.metadata.get('username'),
                content=unified_message.content
            )

            log = EventLog(user_uuid, unified_message.platform)

            if not should_continue:
                if user_name:
                    logger.info(f"user {user_name} successfully onboarded")
                # persist the onboarding exchange like any other turn. without
                # this, the event log stays empty through onboarding, so the
                # scheduler's "no messages yet" rule can't tell "brand new
                # user" apart from "just finished onboarding" - and fires a
                # scheduled check-in within minutes of onboarding completing.
                if unified_message.content:
                    log.append_message("user", "user", unified_message.content)
                if response:
                    log.append_message("agent", AGENT_AUTHOR, response)
                return response

            # record the incoming message first, so it's the last item in history
            user_event = log.append_message("user", "user", unified_message.content)
            if Config.ENABLE_COMPRESSION:
                await self._compress(user_event, user_uuid, unified_message.platform)

            if not self.agent_service:
                return f"echo: {unified_message.content}"

            history = log.recent(self.max_history_messages)
            request = await self.prompt_service.build_conversation_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                tools=self._tool_defs(),
                ambient_context=self._compose_ambient(user_uuid, user_timezone),
            )

            result = await self.agent_service.run(
                request,
                user_uuid=user_uuid,
                platform=unified_message.platform,
                turn_kind="conversation",
            )

            if result.refused or not result.text:
                # don't persist a non-answer into history (nor its actions -
                # they're still in AgentTrace for debugging)
                return REFUSAL_REPLY if result.refused else ERROR_REPLY

            self._record_actions(log, result)
            reply_event = log.append_message("agent", AGENT_AUTHOR, result.text)
            if Config.ENABLE_COMPRESSION:
                await self._compress(reply_event, user_uuid, unified_message.platform)
            return result.text

        except ProviderError as e:
            logger.error(f"provider error while processing message: {e}")
            return ERROR_REPLY
        except Exception as e:
            logger.error(f"error processing message: {e}")
            return "sorry, i encountered an error processing your message."

    async def generate_scheduled_message(self, platform_user_id: str, platform: str) -> Optional[str]:
        """generate a scheduled message for a user"""
        try:
            user_uuid, user_name, should_continue, response, user_timezone = await self._prepare_for_interaction(
                platform=platform,
                platform_user_id=platform_user_id,
                content=None
            )

            # new user -> welcome message; mid-onboarding -> skip (None)
            if not should_continue:
                return response

            if not self.agent_service:
                return None

            log = EventLog(user_uuid, platform)

            history = log.recent(self.max_history_messages)
            request = await self.prompt_service.build_scheduled_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                tools=self._tool_defs(),
                ambient_context=self._compose_ambient(user_uuid, user_timezone),
            )

            result = await self.agent_service.run(
                request,
                user_uuid=user_uuid,
                platform=platform,
                turn_kind="scheduled",
            )

            if result.refused or not result.text:
                return None

            self._record_actions(log, result)
            reply_event = log.append_message("agent", AGENT_AUTHOR, result.text, message_type="scheduled")
            if Config.ENABLE_COMPRESSION:
                await self._compress(reply_event, user_uuid, platform)
            return result.text

        except ProviderError as e:
            logger.error(f"provider error generating scheduled message: {e}")
            return None
        except Exception as e:
            logger.error(f"error generating scheduled message: {e}")
            return None

    async def _compress(self, event: Event, user_uuid: str, platform: str) -> None:
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
                user_uuid=user_uuid,
                platform=platform,
                role=event.role,
                original_content=event.content,
                compressed_content=compressed,
            )
        except Exception as e:
            logger.error(f"compression failed (continuing uncompressed): {e}")
