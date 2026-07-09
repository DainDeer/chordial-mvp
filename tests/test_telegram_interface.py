"""telegram interface tests: handler policy and outbound error mapping, with
ptb's Application/bot faked (no network, no token).

the properties that matter:
- unknown senders NEVER reach chat_service (no user creation, no api spend):
  code-shaped text attempts redemption, everything else gets one static line
- /start with a deep-link payload redeems; bare /start is polite
- outbound maps telegram errors onto the router's contract: Forbidden and
  chat-not-found -> UndeliverableError (permanent), other TelegramError ->
  False (transient), RetryAfter honored once
- chunking at 4096 with pacing between chunks
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError  # noqa: E402

from config import Config  # noqa: E402

# the interface reads TELEGRAM_TOKEN at construction; give it something
Config.TELEGRAM_TOKEN = Config.TELEGRAM_TOKEN or "123456:TEST-token"

from src.providers.platforms.telegram_bot import (  # noqa: E402
    TelegramInterface, STRANGER_REPLY, LINKED_REPLY, INVALID_CODE_REPLY,
    ALREADY_LINKED_REPLY, _TELEGRAM_MAX_LENGTH,
)
from src.providers.platforms.base import UndeliverableError  # noqa: E402
from src.services.platform_link_service import LinkOutcome, LinkResult  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- fakes ---------------------------------------------------------------------

class FakeBot:
    def __init__(self, send_error=None, retry_once=False):
        self.sent = []            # text chunks sent via send_message
        self.actions = []         # chat actions
        self.send_error = send_error
        self._retry_once = retry_once

    async def send_message(self, chat_id, text):
        if self._retry_once:
            self._retry_once = False
            raise RetryAfter(0)   # 0-second wait keeps the test fast
        if self.send_error:
            raise self.send_error
        self.sent.append((chat_id, text))

    async def send_chat_action(self, chat_id, action):
        if self.send_error and isinstance(self.send_error, (Forbidden, BadRequest)):
            raise self.send_error
        self.actions.append((chat_id, action))


class FakeChatService:
    def __init__(self, reply="hi from chordial!"):
        self.reply = reply
        self.received = []

    async def process_message(self, unified):
        self.received.append(unified)
        return self.reply


class FakeUserManager:
    def __init__(self, known_ids=()):
        self.known = set(known_ids)

    async def is_new_user(self, platform, platform_user_id):
        return platform_user_id not in self.known


class FakeLinkService:
    def __init__(self, result=LinkResult.LINKED):
        self.result = result
        self.redeemed = []

    async def redeem(self, code, platform, platform_user_id, username=None):
        self.redeemed.append((code, platform, platform_user_id, username))
        return LinkOutcome(self.result, user_uuid="u1" if self.result in
                           (LinkResult.LINKED, LinkResult.RELINKED) else None)


def _interface(chat=None, links=None, users=None, bot=None):
    iface = TelegramInterface(
        chat or FakeChatService(),
        links if links is not None else FakeLinkService(),
        users or FakeUserManager(),
    )
    iface.app = types.SimpleNamespace(bot=bot or FakeBot())
    return iface


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.message_id = 42
        self.date = None
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeChat:
    def __init__(self):
        self.sent = []
        self.actions = []

    async def send_message(self, text):
        self.sent.append(text)

    async def send_action(self, action):
        self.actions.append(action)


def _update(text, user_id=777, username="wanderer"):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=user_id, username=username),
        effective_chat=FakeChat(),
        message=FakeMessage(text),
    )


def _ctx(args=None):
    return types.SimpleNamespace(args=args or [])


# --- inbound: stranger policy -----------------------------------------------------

def test_stranger_gets_static_reply_and_never_reaches_chat_service():
    chat = FakeChatService()
    iface = _interface(chat=chat)
    update = _update("hello?? who are you")
    run(iface._on_message(update, _ctx()))

    assert update.message.replies == [STRANGER_REPLY]
    assert chat.received == []   # the load-bearing assertion


def test_stranger_code_shaped_text_attempts_redemption():
    links = FakeLinkService(LinkResult.LINKED)
    iface = _interface(links=links)
    update = _update("  abcd2345 ")   # lowercased + padded; normalization's job
    run(iface._on_message(update, _ctx()))

    assert links.redeemed == [("ABCD2345", "telegram", "777", "wanderer")]
    assert update.message.replies == [LINKED_REPLY]


def test_stranger_bad_code_gets_invalid_reply():
    links = FakeLinkService(LinkResult.INVALID)
    iface = _interface(links=links)
    update = _update("ABCD2345")
    run(iface._on_message(update, _ctx()))
    assert update.message.replies == [INVALID_CODE_REPLY]


def test_known_user_flows_to_chat_service_with_reply():
    chat = FakeChatService(reply="hey dain!")
    iface = _interface(chat=chat, users=FakeUserManager(known_ids={"777"}))
    update = _update("good morning!")
    run(iface._on_message(update, _ctx()))

    assert len(chat.received) == 1
    unified = chat.received[0]
    assert unified.platform == "telegram"
    assert unified.platform_user_id == "777"
    assert update.effective_chat.sent == ["hey dain!"]
    assert update.effective_chat.actions  # typing indicator fired


# --- inbound: /start ---------------------------------------------------------------

def test_start_with_payload_redeems():
    links = FakeLinkService(LinkResult.LINKED)
    iface = _interface(links=links)
    update = _update("/start")
    run(iface._on_start(update, _ctx(args=["ABCD2345"])))
    assert links.redeemed[0][0] == "ABCD2345"
    assert update.message.replies == [LINKED_REPLY]


def test_bare_start_stranger_vs_known():
    iface = _interface()
    update = _update("/start")
    run(iface._on_start(update, _ctx()))
    assert update.message.replies == [STRANGER_REPLY]

    iface = _interface(users=FakeUserManager(known_ids={"777"}))
    update = _update("/start")
    run(iface._on_start(update, _ctx()))
    assert update.message.replies == [ALREADY_LINKED_REPLY]


# --- outbound -----------------------------------------------------------------------

def test_send_message_chunks_and_paces():
    bot = FakeBot()
    iface = _interface(bot=bot)
    long = ("x" * 3000) + "\n\n" + ("y" * 3000)
    ok = run(iface.send_message("777", long))
    assert ok is True
    assert len(bot.sent) == 2
    assert all(len(text) <= _TELEGRAM_MAX_LENGTH for _, text in bot.sent)
    assert bot.actions  # typing indicator before the sends


def test_forbidden_raises_undeliverable():
    bot = FakeBot(send_error=Forbidden("bot was blocked by the user"))
    iface = _interface(bot=bot)
    with pytest.raises(UndeliverableError):
        run(iface.send_message("777", "hello"))


def test_chat_not_found_raises_undeliverable():
    bot = FakeBot(send_error=BadRequest("Chat not found"))
    iface = _interface(bot=bot)
    with pytest.raises(UndeliverableError):
        run(iface.send_message("777", "hello"))


def test_malformed_id_raises_undeliverable():
    iface = _interface()
    with pytest.raises(UndeliverableError):
        run(iface.send_message("not-a-number", "hello"))


def test_transient_error_returns_false():
    bot = FakeBot(send_error=TelegramError("gateway hiccup"))
    iface = _interface(bot=bot)
    assert run(iface.send_message("777", "hello")) is False


def test_retry_after_is_honored_once():
    bot = FakeBot(retry_once=True)
    iface = _interface(bot=bot)
    ok = run(iface.send_message("777", "hello"))
    assert ok is True
    assert len(bot.sent) == 1  # succeeded on the retry
