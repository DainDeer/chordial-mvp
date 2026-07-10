"""the platform adapter: the boundary between platform interfaces and the
orchestrator.

owns what's platform-shaped - unwrapping UnifiedMessage, deciding whether an
activation is a fresh introduction or an ordinary turn, and the in-character
copy for non-answers. everything about deciding who speaks and recording what
happened lives in the Orchestrator; everything about how a persona thinks
lives in its agent; everything about how the storytelling introduction is
run lives in the model, guided by PromptService.build_introduction_request.

v1's rigid onboarding state machine (name -> timezone -> memory) is retired.
"completeness" is no longer tracked here at all: it's chordial's own
HelperState.status, exactly like any other helper's relationship state. a
brand-new user (or one whose chordial relationship hasn't reached 'active'
yet) gets an `introduction` stimulus instead of a `user_message` one; the
orchestrator routes that to the right helper with an introduction briefing,
and the model runs the whole ritual conversationally, calling
complete_introduction when it's done.
"""
from typing import Optional
import logging

from src.models.unified_message import UnifiedMessage
from src.managers.helper_state_manager import HelperStateManager
from src.managers.user_manager import UserManager
from src.services.orchestrator import Stimulus

logger = logging.getLogger(__name__)

# in-character copy for the two non-response outcomes. never persisted to
# history (so they can't pollute future context).
REFUSAL_REPLY = "i don't think i can help with that one, but i'm here for whatever else is on your mind 💛"
ERROR_REPLY = "i'm having a little trouble reaching my thoughts right now — mind trying again in a bit?"

# a chordial relationship in either of these states means the user hasn't
# finished the front-door introduction yet.
_STILL_INTRODUCING = {"not_met", "introducing"}


class ChatService:
    """handles incoming platform messages: introduction vs. ordinary turns,
    then orchestration."""

    def __init__(
        self,
        orchestrator=None,
        user_manager=None,
    ):
        self.orchestrator = orchestrator
        self.user_manager = user_manager or UserManager()
        self.helper_states = HelperStateManager()

    async def process_message(self, unified_message: UnifiedMessage) -> Optional[str]:
        """process an incoming message and generate a response.

        returns None when the activation was handled out-of-band (group
        scope - each speaker already delivered its own line via its own
        bot); otherwise the reply string for the receiving interface to send.
        """
        try:
            platform = unified_message.platform
            platform_user_id = unified_message.platform_user_id
            username = (unified_message.metadata or {}).get("username")

            chat_scope = getattr(unified_message, "chat_scope", "dm") or "dm"
            group_chat_id = getattr(unified_message, "group_chat_id", None)
            dm_helper = getattr(unified_message, "dm_helper", None) or "chordial"
            mentioned = getattr(unified_message, "mentioned", None) or []

            user_uuid, user_name = await self.user_manager.get_or_create_user(
                platform, platform_user_id, username
            )
            user_timezone = await self.user_manager.get_user_timezone(user_uuid)

            kind = "user_message"
            intro_helper = None
            if chat_scope == "dm":
                chordial_state = await self.helper_states.get(user_uuid, "chordial")
                # legacy signal: a pre-v3 user who never got a HelperState row
                # but also never finished the old name step - treat the same
                # as "still introducing" so nobody gets stranded mid-migration.
                still_introducing = (
                    chordial_state.status in _STILL_INTRODUCING or user_name is None
                )
                if still_introducing:
                    kind = "introduction"
                    intro_helper = dm_helper
                    if chordial_state.status != "active":
                        await self.helper_states.set_status(user_uuid, "chordial", "introducing")

            if not self.orchestrator:
                return f"echo: {unified_message.content}"

            deliverable = await self.orchestrator.handle(Stimulus(
                kind=kind,
                user_uuid=user_uuid,
                platform=platform,
                content=unified_message.content,
                user_name=user_name,
                user_timezone=user_timezone,
                chat_scope=chat_scope,
                group_chat_id=group_chat_id,
                dm_helper=dm_helper,
                mentioned=mentioned,
                intro_helper=intro_helper,
            ))

            return self._reply_for(deliverable)

        except Exception as e:
            logger.error(f"error processing message: {e}")
            return "sorry, i encountered an error processing your message."

    async def begin_introduction(
        self, platform: str, platform_user_id: str, helper_id: str
    ) -> Optional[str]:
        """entry point for the meet-the-guides deep link (`t.me/<bot>?start=meet`):
        a user already known on `platform` taps a guide's link, which opens
        that helper's dm and should kick off ITS introduction. flips the
        helper's relationship to 'introducing' and runs the activation.
        """
        try:
            user_uuid, user_name = await self.user_manager.get_or_create_user(
                platform, platform_user_id
            )
            user_timezone = await self.user_manager.get_user_timezone(user_uuid)

            await self.helper_states.set_status(user_uuid, helper_id, "introducing")

            if not self.orchestrator:
                return f"echo: meet {helper_id}"

            deliverable = await self.orchestrator.handle(Stimulus(
                kind="introduction",
                user_uuid=user_uuid,
                platform=platform,
                content=None,
                user_name=user_name,
                user_timezone=user_timezone,
                chat_scope="dm",
                dm_helper=helper_id,
                intro_helper=helper_id,
            ))

            return self._reply_for(deliverable)

        except Exception as e:
            logger.error(f"error beginning introduction for helper {helper_id}: {e}")
            return "sorry, i encountered an error processing your message."

    @staticmethod
    def _reply_for(deliverable) -> Optional[str]:
        """map a Deliverable to what the platform interface should send.
        `handled` (group scope: each line already went out via its own bot)
        means nothing more to send from here."""
        if deliverable.handled:
            return None
        if deliverable.refused:
            return REFUSAL_REPLY
        if deliverable.errored or not deliverable.text:
            return ERROR_REPLY
        return deliverable.text
