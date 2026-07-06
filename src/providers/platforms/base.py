from abc import ABC, abstractmethod
from typing import Any


class UndeliverableError(Exception):
    """raised by an interface when a recipient is *permanently* unreachable on
    this platform (e.g. discord 404 unknown-user, or 403 dms-forbidden) - as
    opposed to a transient failure (network blip, rate limit). the router uses
    this distinction to decide whether to deactivate the platform link."""


class BaseInterface(ABC):
    """abstract base class for all chat interfaces (discord, telegram, web, etc)"""

    # platform name this interface serves ('discord', 'telegram', ...). used by
    # the router to map an outbound (platform, user) to the right interface.
    platform: str = ""

    def __init__(self, chat_service):
        self.chat_service = chat_service

    @abstractmethod
    async def start(self):
        """start the interface"""
        pass

    @abstractmethod
    async def stop(self):
        """stop the interface"""
        pass

    @abstractmethod
    async def send_message(self, platform_user_id: str, content: str, **kwargs) -> bool:
        """send a message to a user. return True on success. raise
        UndeliverableError if the recipient is permanently unreachable so the
        router can deactivate the link; other exceptions are treated as
        transient and leave the link active."""
        pass

    @abstractmethod
    async def handle_incoming_message(self, message: Any):
        """handle an incoming message from the platform"""
        pass
