"""outbound message router: the one place that knows how to deliver a message
to a (platform, user).

replaces the hand-rolled callback in main.py that string-matched the platform
and sniffed `hasattr(interface, 'send_message')`. interfaces register by their
`platform` name; the scheduler (or anything else) calls `deliver(...)` without
knowing which interface backs a platform. when a send is permanently
undeliverable, the router deactivates that platform link so we stop paying to
generate messages for a dead channel.
"""
import logging
from typing import Dict

from src.providers.platforms.base import BaseInterface, UndeliverableError
from src.managers.user_manager import UserManager

logger = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, user_manager: UserManager):
        self._interfaces: Dict[str, BaseInterface] = {}
        self._user_manager = user_manager

    def register(self, interface: BaseInterface) -> None:
        platform = interface.platform
        if not platform:
            raise ValueError(
                f"{type(interface).__name__} must set a `platform` name to register"
            )
        if platform in self._interfaces:
            logger.warning("overwriting already-registered interface for '%s'", platform)
        self._interfaces[platform] = interface
        logger.info("registered interface for platform '%s'", platform)

    def platforms(self) -> list[str]:
        """platforms that currently have a live interface - the scheduler drives
        its loop off this instead of a hardcoded list."""
        return list(self._interfaces.keys())

    async def deliver(self, platform: str, platform_user_id: str, message: str) -> bool:
        """send `message` to `platform_user_id` on `platform`. returns True on
        success. on a permanent failure, deactivates the platform link and
        returns False. matches the (platform, user_id, message) callback shape
        the scheduler expects."""
        interface = self._interfaces.get(platform)
        if interface is None:
            logger.error("no interface registered for platform '%s'", platform)
            return False

        try:
            return await interface.send_message(platform_user_id, message)
        except UndeliverableError as e:
            logger.warning(
                "permanent delivery failure for %s:%s - %s; deactivating link",
                platform, platform_user_id, e,
            )
            await self._user_manager.deactivate_platform_identity(platform, platform_user_id)
            return False
