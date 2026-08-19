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
    def __init__(
        self,
        platform: str,
        *,
        helper_id=None,
        raise_exc: Exception = None,
        ok: bool = True,
    ):
        self.platform = platform
        if helper_id is not None:
            self.helper_id = helper_id
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
    def __init__(self, links=None):
        self.deactivated = []
        # (platform, platform_user_id) -> user_uuid, for the tether mirror
        self.links = links or {}

    async def deactivate_platform_identity(self, platform, platform_user_id):
        self.deactivated.append((platform, platform_user_id))

    async def lookup_user_uuid(self, platform, platform_user_id):
        return self.links.get((platform, platform_user_id))


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


# --- v3: per-helper interfaces + speaker-aware delivery ---------------------------


def test_deliver_as_routes_to_the_speakers_bot():
    router = MessageRouter(FakeUserManager())
    skip = FakeInterface("telegram", helper_id="skip")
    juniper = FakeInterface("telegram", helper_id="juniper")
    router.register(skip)
    router.register(juniper)

    ok = run(router.deliver_as("telegram", "-100", "hi from skip", speaker="skip"))

    assert ok is True
    assert skip.sent == [("-100", "hi from skip")]
    assert juniper.sent == []  # only the addressed speaker's bot sent


def test_deliver_as_falls_back_to_single_bot_platform():
    # discord registers with no helper_id -> (discord, None); a speaker that has
    # no dedicated bot still resolves to the single-bot interface.
    router = MessageRouter(FakeUserManager())
    discord = FakeInterface("discord")
    router.register(discord)

    ok = run(router.deliver_as("discord", "42", "hi", speaker="skip"))

    assert ok is True
    assert discord.sent == [("42", "hi")]


def test_deliver_as_fails_closed_when_speakers_bot_is_unavailable():
    # no exact speaker match and no (platform, None) entry: do not impersonate
    # mabel through skip's distinct Telegram bot account.
    router = MessageRouter(FakeUserManager())
    skip = FakeInterface("telegram", helper_id="skip")
    router.register(skip)

    ok = run(router.deliver_as("telegram", "-100", "hi", speaker="mabel"))

    assert ok is False
    assert skip.sent == []


def test_deliver_defaults_speaker_to_chordial():
    router = MessageRouter(FakeUserManager())
    vel = FakeInterface("telegram", helper_id="vel")
    skip = FakeInterface("telegram", helper_id="skip")
    router.register(vel)
    router.register(skip)

    ok = run(router.deliver("telegram", "555", "legacy hook"))

    assert ok is True
    assert vel.sent == [("555", "legacy hook")]
    assert skip.sent == []


def test_platforms_dedupes_across_per_helper_interfaces():
    router = MessageRouter(FakeUserManager())
    router.register(FakeInterface("telegram", helper_id="vel"))
    router.register(FakeInterface("telegram", helper_id="skip"))
    router.register(FakeInterface("discord"))
    assert set(router.platforms()) == {"telegram", "discord"}
    assert router.platforms().count("telegram") == 1


def test_deliver_as_deactivates_link_on_permanent_failure():
    users = FakeUserManager()
    router = MessageRouter(users)
    router.register(
        FakeInterface(
            "telegram", helper_id="skip", raise_exc=UndeliverableError("blocked")
        )
    )

    ok = run(router.deliver_as("telegram", "dead", "hi", speaker="skip"))

    assert ok is False
    assert users.deactivated == [("telegram", "dead")]


# --- 7a: the tether mirror (one conversation, two windows) ------------------------


class FakeAppInterface(FakeInterface):
    """the (app, None) interface, kwargs kept - the mirror's payload extras
    are the point."""

    def __init__(self, *, raise_exc=None):
        super().__init__("app", raise_exc=raise_exc)
        self.kwargs = []

    async def send_message(self, platform_user_id, content, **kwargs):
        if self._raise is not None:
            raise self._raise
        self.sent.append((platform_user_id, content))
        self.kwargs.append(kwargs)
        return True


def test_confirmed_telegram_delivery_mirrors_to_the_app():
    users = FakeUserManager(links={("telegram", "777"): "u-777"})
    router = MessageRouter(users)
    telegram = FakeInterface("telegram")
    app = FakeAppInterface()
    router.register(telegram)
    router.register(app)

    ok = run(router.deliver_as("telegram", "777", "burrito logged",
                               speaker="remy", stream_id="room-1"))

    assert ok is True
    assert telegram.sent == [("777", "burrito logged")]
    assert app.sent == [("u-777", "burrito logged")]
    extras = app.kwargs[0]
    assert extras["speaker"] == "remy"
    assert extras["stream_id"] == "room-1"
    assert extras["source_platform"] == "telegram"


def test_failed_telegram_delivery_never_mirrors():
    users = FakeUserManager(links={("telegram", "777"): "u-777"})
    router = MessageRouter(users)
    router.register(FakeInterface("telegram", ok=False))
    app = FakeAppInterface()
    router.register(app)

    ok = run(router.deliver_as("telegram", "777", "hi", speaker="vel"))

    assert ok is False
    assert app.sent == []  # an unconfirmed line must not appear anywhere


def test_discord_delivery_does_not_mirror():
    """the mirror is the TETHER's contract; discord stays fully pre-7a."""
    users = FakeUserManager(links={("discord", "42"): "u-42"})
    router = MessageRouter(users)
    discord = FakeInterface("discord")
    app = FakeAppInterface()
    router.register(discord)
    router.register(app)

    ok = run(router.deliver_as("discord", "42", "hi", speaker="vel"))

    assert ok is True
    assert app.sent == []


def test_mirror_without_linked_user_or_app_interface_noops():
    # no app interface registered at all
    users = FakeUserManager(links={("telegram", "777"): "u-777"})
    router = MessageRouter(users)
    telegram = FakeInterface("telegram")
    router.register(telegram)
    assert run(router.deliver_as("telegram", "777", "hi", speaker="vel")) is True

    # app registered, but the telegram id maps to nobody
    router2 = MessageRouter(FakeUserManager())
    router2.register(FakeInterface("telegram"))
    app = FakeAppInterface()
    router2.register(app)
    assert run(router2.deliver_as("telegram", "999", "hi", speaker="vel")) is True
    assert app.sent == []


def test_mirror_failure_never_touches_the_delivery_verdict():
    users = FakeUserManager(links={("telegram", "777"): "u-777"})
    router = MessageRouter(users)
    telegram = FakeInterface("telegram")
    router.register(telegram)
    router.register(FakeAppInterface(raise_exc=RuntimeError("mirror cracked")))

    ok = run(router.deliver_as("telegram", "777", "hi", speaker="vel"))

    assert ok is True                    # telegram confirmed; the echo is extra
    assert telegram.sent == [("777", "hi")]
