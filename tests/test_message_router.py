"""router tests: routing, and link-deactivation on permanent delivery failure.

no database - the router's collaborators (interface, user_manager) are faked so
these stay pure-logic and fast. follows the repo's plain-asyncio test style
(no pytest-asyncio dependency).
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.message_router import MessageRouter  # noqa: E402
from src.providers.platforms.base import BaseInterface, UndeliverableError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeInterface(BaseInterface):
    def __init__(self, platform: str, *, raise_exc: Exception = None, ok: bool = True):
        self.platform = platform
        self._raise = raise_exc
        self._ok = ok
        self.sent = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_message(self, platform_user_id, content, **kwargs):
        if self._raise is not None:
            raise self._raise
        self.sent.append((platform_user_id, content))
        return self._ok

    async def handle_incoming_message(self, message):
        pass


class FakeUserManager:
    def __init__(self):
        self.deactivated = []

    async def deactivate_platform_identity(self, platform, platform_user_id):
        self.deactivated.append((platform, platform_user_id))


def test_delivers_to_the_registered_interface():
    users = FakeUserManager()
    router = MessageRouter(users)
    discord = FakeInterface("discord")
    router.register(discord)

    ok = run(router.deliver("discord", "42", "hi"))

    assert ok is True
    assert discord.sent == [("42", "hi")]
    assert users.deactivated == []


def test_platforms_reflects_registered_interfaces():
    router = MessageRouter(FakeUserManager())
    router.register(FakeInterface("discord"))
    router.register(FakeInterface("telegram"))
    assert set(router.platforms()) == {"discord", "telegram"}


def test_unknown_platform_is_a_noop_failure():
    users = FakeUserManager()
    router = MessageRouter(users)
    router.register(FakeInterface("discord"))

    ok = run(router.deliver("telegram", "42", "hi"))

    assert ok is False
    assert users.deactivated == []  # nothing to deactivate, no interface


def test_permanent_failure_deactivates_the_link():
    users = FakeUserManager()
    router = MessageRouter(users)
    router.register(FakeInterface("discord", raise_exc=UndeliverableError("gone")))

    ok = run(router.deliver("discord", "dead-id", "hi"))

    assert ok is False
    assert users.deactivated == [("discord", "dead-id")]


def test_transient_failure_does_not_deactivate():
    users = FakeUserManager()
    router = MessageRouter(users)
    # send_message swallowed a transient error and returned False (no raise)
    router.register(FakeInterface("discord", ok=False))

    ok = run(router.deliver("discord", "42", "hi"))

    assert ok is False
    assert users.deactivated == []  # transient - keep the link active


def test_register_requires_a_platform_name():
    router = MessageRouter(FakeUserManager())
    with pytest.raises(ValueError):
        router.register(FakeInterface(""))
