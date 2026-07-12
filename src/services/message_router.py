"""outbound message router: the one place that knows how to deliver a message
to a (platform, user) - and, in v3, as a specific helper.

replaces the hand-rolled callback in main.py that string-matched the platform
and sniffed `hasattr(interface, 'send_message')`. interfaces register by their
`platform` name; the scheduler (or anything else) calls `deliver(...)` without
knowing which interface backs a platform. when a send is permanently
undeliverable, the router deactivates that platform link so we stop paying to
generate messages for a dead channel.

v3 runs N telegram interfaces (one bot per helper), all reporting
`platform == "telegram"`. keying purely by platform would collide, so the
router keys on `(platform, helper_id)` - the interface's `helper_id` attr, or
`None` for single-bot platforms like discord. `deliver_as(..., speaker=...)`
resolves the speaking helper's bot; the legacy 3-arg `deliver` speaks as
chordial.
"""

import logging
from typing import Dict, Optional, Tuple

from src.providers.platforms.base import BaseInterface, UndeliverableError
from src.managers.user_manager import UserManager

logger = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, user_manager: UserManager):
        # keyed by (platform, helper_id); helper_id is None for single-bot
        # platforms (discord). one telegram entry per helper bot.
        self._interfaces: Dict[Tuple[str, Optional[str]], BaseInterface] = {}
        self._user_manager = user_manager

    def register(self, interface: BaseInterface) -> None:
        platform = interface.platform
        if not platform:
            raise ValueError(
                f"{type(interface).__name__} must set a `platform` name to register"
            )
        helper_id = getattr(interface, "helper_id", None)
        key = (platform, helper_id)
        if key in self._interfaces:
            logger.warning("overwriting already-registered interface for %s", key)
        self._interfaces[key] = interface
        logger.info(
            "registered interface for platform '%s' (helper=%s)", platform, helper_id
        )

    def platforms(self) -> list[str]:
        """distinct platforms that currently have a live interface - the
        scheduler drives its loop off this instead of a hardcoded list. deduped
        across the per-helper interfaces a platform may have."""
        seen: list[str] = []
        for platform, _helper_id in self._interfaces:
            if platform not in seen:
                seen.append(platform)
        return seen

    def _resolve(
        self, platform: str, speaker: Optional[str]
    ) -> Optional[BaseInterface]:
        """find the interface that should send as `speaker` on `platform`:
        the exact (platform, speaker) bot, else the platform's single-bot
        (platform, None) interface. Platforms with helper-specific interfaces
        fail closed if the requested speaker is unavailable, rather than
        impersonating that helper through a different bot account."""
        interface = self._interfaces.get((platform, speaker))
        if interface is not None:
            return interface
        interface = self._interfaces.get((platform, None))
        if interface is not None:
            return interface
        return None

    async def deliver_as(
        self, platform: str, target_id: str, message: str, speaker: str
    ) -> bool:
        """send `message` to `target_id` on `platform` AS `speaker` (a helper
        id). resolves the speaker's bot (falling back only to the platform's
        single-bot interface), returns True on success. on a permanent
        failure, deactivates the platform link and returns False."""
        interface = self._resolve(platform, speaker)
        if interface is None:
            logger.error(
                "no interface registered for platform '%s' (speaker=%s)",
                platform,
                speaker,
            )
            return False

        try:
            return await interface.send_message(target_id, message)
        except UndeliverableError as e:
            logger.warning(
                "permanent delivery failure for %s:%s (speaker=%s) - %s; deactivating link",
                platform,
                target_id,
                speaker,
                e,
            )
            await self._user_manager.deactivate_platform_identity(platform, target_id)
            return False

    async def deliver(self, platform: str, platform_user_id: str, message: str) -> bool:
        """send `message` to `platform_user_id` on `platform`, as chordial. the
        legacy 3-arg hook the scheduler and the orchestrator's switch-notice
        call; delegates to deliver_as with the default speaker."""
        return await self.deliver_as(
            platform, platform_user_id, message, speaker="chordial"
        )
