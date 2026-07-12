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

import asyncio
import logging
from typing import Optional
from weakref import WeakValueDictionary

from src.models.unified_message import UnifiedMessage
from src.managers.helper_state_manager import HelperStateManager
from src.managers.user_manager import UserManager
from src.services.orchestrator import Stimulus

logger = logging.getLogger(__name__)

# in-character copy for the two non-response outcomes. never persisted to
# history (so they can't pollute future context).
REFUSAL_REPLY = "i don't think i can help with that one, but i'm here for whatever else is on your mind 💛"
ERROR_REPLY = "i'm having a little trouble reaching my thoughts right now — mind trying again in a bit?"


def _still_introducing(chordial_status: str, user_name: Optional[str]) -> bool:
    """should this dm turn run the front-door introduction, or is it an
    ordinary turn?

    keyed on chordial's relationship status, NOT on preferred_name: once
    chordial is 'active' the intro is DONE even if the name never got
    persisted (otherwise a stuck None name re-loops onboarding forever - the
    bug that shipped in the first phase-2 cut). the legacy `user_name is None`
    signal only applies BEFORE any relationship exists (status 'not_met'), so
    a pre-v3 user who already has a name isn't dragged back into the forest,
    while a brand-new user with no name still gets introduced."""
    if chordial_status == "active":
        return False
    if chordial_status == "introducing":
        return True
    # not_met (no row yet) or an odd declined/disabled front door: introduce
    # only if we don't already know their name.
    return user_name is None


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
        # Serialize a user's complete turn across every platform/helper while
        # still allowing different users to run concurrently. Weak values keep
        # this registry from growing forever as one-off users come and go; a
        # waiter/holder retains the lock strongly for as long as it is needed.
        self._user_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _lock_for_user(self, user_uuid: str) -> asyncio.Lock:
        lock = self._user_locks.get(user_uuid)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_uuid] = lock
        return lock

    async def process_message(self, unified_message: UnifiedMessage) -> Optional[str]:
        """process an incoming message and generate a response.

        returns None when the activation was delivered through the router
        (production dms and group lines); otherwise returns the reply string
        for an isolated/synchronous interface to send.
        """
        try:
            platform = unified_message.platform
            platform_user_id = unified_message.platform_user_id
            username = (unified_message.metadata or {}).get("username")

            chat_scope = getattr(unified_message, "chat_scope", "dm") or "dm"
            group_chat_id = getattr(unified_message, "group_chat_id", None)
            dm_helper = getattr(unified_message, "dm_helper", None) or "chordial"
            mentioned = getattr(unified_message, "mentioned", None) or []

            user_uuid, _ = await self.user_manager.get_or_create_user(
                platform, platform_user_id, username
            )
            async with self._lock_for_user(user_uuid):
                # Refresh inside the lock: an earlier queued turn may have
                # learned the user's name or timezone while this turn waited.
                user_name, user_timezone = await self.user_manager.get_user_profile(
                    user_uuid
                )

                kind = "user_message"
                intro_helper = None
                if chat_scope == "dm":
                    helper_state = await self.helper_states.get(user_uuid, dm_helper)
                    if dm_helper == "chordial":
                        is_introducing = _still_introducing(
                            helper_state.status, user_name
                        )
                    else:
                        # Preferred-name back compatibility belongs only to the
                        # chordial front door. A specialist continues its own
                        # multi-turn ritual only while its relationship says so.
                        is_introducing = helper_state.status == "introducing"
                    if is_introducing:
                        kind = "introduction"
                        intro_helper = dm_helper
                        if helper_state.status != "introducing":
                            await self.helper_states.set_status(
                                user_uuid, dm_helper, "introducing"
                            )

                if not self.orchestrator:
                    return f"echo: {unified_message.content}"

                deliverable = await self.orchestrator.handle(
                    Stimulus(
                        kind=kind,
                        user_uuid=user_uuid,
                        platform=platform,
                        content=unified_message.content,
                        delivery_target_id=platform_user_id,
                        user_name=user_name,
                        user_timezone=user_timezone,
                        chat_scope=chat_scope,
                        group_chat_id=group_chat_id,
                        dm_helper=dm_helper,
                        mentioned=mentioned,
                        intro_helper=intro_helper,
                    )
                )

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
            user_uuid, _ = await self.user_manager.get_or_create_user(
                platform, platform_user_id
            )
            async with self._lock_for_user(user_uuid):
                user_name, user_timezone = await self.user_manager.get_user_profile(
                    user_uuid
                )

                await self.helper_states.set_status(user_uuid, helper_id, "introducing")

                if not self.orchestrator:
                    return f"echo: meet {helper_id}"

                deliverable = await self.orchestrator.handle(
                    Stimulus(
                        kind="introduction",
                        user_uuid=user_uuid,
                        platform=platform,
                        content=None,
                        delivery_target_id=platform_user_id,
                        user_name=user_name,
                        user_timezone=user_timezone,
                        chat_scope="dm",
                        dm_helper=helper_id,
                        intro_helper=helper_id,
                    )
                )

                return self._reply_for(deliverable)

        except Exception as e:
            logger.error(f"error beginning introduction for helper {helper_id}: {e}")
            return "sorry, i encountered an error processing your message."

    @staticmethod
    def _reply_for(deliverable) -> Optional[str]:
        """map a Deliverable to what the platform interface should send.
        `handled` means the router already confirmed delivery, so the calling
        interface must not send a duplicate."""
        if deliverable.handled:
            return None
        if deliverable.refused:
            return REFUSAL_REPLY
        if deliverable.errored or not deliverable.text:
            return ERROR_REPLY
        return deliverable.text
