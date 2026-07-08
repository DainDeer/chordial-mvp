"""the platform adapter: the boundary between platform interfaces and the
orchestrator.

owns what's platform-shaped - unwrapping UnifiedMessage, the onboarding state
machine, and the in-character copy for non-answers. everything about deciding
who speaks and recording what happened lives in the Orchestrator; everything
about how the persona thinks lives in its agent.
"""
from typing import Optional
import logging

from src.models.unified_message import UnifiedMessage
from src.managers.event_log import EventLog
from src.managers.user_manager import UserManager
from src.services.onboarding_service import OnboardingService
from src.services.orchestrator import Stimulus

logger = logging.getLogger(__name__)

# in-character copy for the two non-response outcomes. never persisted to
# history (so they can't pollute future context).
REFUSAL_REPLY = "i don't think i can help with that one, but i'm here for whatever else is on your mind 💛"
ERROR_REPLY = "i'm having a little trouble reaching my thoughts right now — mind trying again in a bit?"

# onboarding replies are authored by the chat persona
AGENT_AUTHOR = "chordial"


class ChatService:
    """handles incoming platform messages: onboarding, then orchestration."""

    def __init__(
        self,
        orchestrator=None,
        user_manager=None,
    ):
        self.orchestrator = orchestrator
        self.user_manager = user_manager or UserManager()
        self.onboarding_service = OnboardingService(self.user_manager)

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
          callers don't each re-fetch it from the database for the same
          interaction
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

            if not should_continue:
                if user_name:
                    logger.info(f"user {user_name} successfully onboarded")
                # persist the onboarding exchange like any other turn. without
                # this, the event log stays empty through onboarding, so the
                # scheduler's "no messages yet" rule can't tell "brand new
                # user" apart from "just finished onboarding" - and fires a
                # scheduled check-in within minutes of onboarding completing.
                log = EventLog(user_uuid, unified_message.platform)
                if unified_message.content:
                    log.append_message("user", "user", unified_message.content)
                if response:
                    log.append_message("agent", AGENT_AUTHOR, response)
                return response

            if not self.orchestrator:
                return f"echo: {unified_message.content}"

            deliverable = await self.orchestrator.handle(Stimulus(
                kind="user_message",
                user_uuid=user_uuid,
                platform=unified_message.platform,
                content=unified_message.content,
                user_name=user_name,
                user_timezone=user_timezone,
            ))

            if deliverable.refused:
                return REFUSAL_REPLY
            if deliverable.errored or not deliverable.text:
                return ERROR_REPLY
            return deliverable.text

        except Exception as e:
            logger.error(f"error processing message: {e}")
            return "sorry, i encountered an error processing your message."
