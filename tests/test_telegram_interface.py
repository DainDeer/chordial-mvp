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

from telegram.error import (
    BadRequest,
    Forbidden,
    RetryAfter,
    TelegramError,
)  # noqa: E402

from config import Config  # noqa: E402

# the interface reads TELEGRAM_TOKEN at construction; give it something
Config.TELEGRAM_TOKEN = Config.TELEGRAM_TOKEN or "123456:TEST-token"

from src.providers.platforms.telegram_bot import (  # noqa: E402
    TelegramInterface,
    UpdateDeduper,
    mentioned_helpers,
    STRANGER_REPLY,
    LINKED_REPLY,
    INVALID_CODE_REPLY,
    ALREADY_LINKED_REPLY,
    _TELEGRAM_MAX_LENGTH,
)
from src.providers.platforms.base import UndeliverableError  # noqa: E402
from src.services.platform_link_service import LinkOutcome, LinkResult  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- fakes ---------------------------------------------------------------------


class FakeBot:
    def __init__(self, send_error=None, retry_once=False):
        self.sent = []  # text chunks sent via send_message
        self.actions = []  # chat actions
        self.send_error = send_error
        self._retry_once = retry_once

    async def send_message(self, chat_id, text):
        if self._retry_once:
            self._retry_once = False
            raise RetryAfter(0)  # 0-second wait keeps the test fast
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
        self.introductions = []  # (platform, platform_user_id, helper_id)

    async def process_message(self, unified):
        self.received.append(unified)
        return self.reply

    async def begin_introduction(self, platform, platform_user_id, helper_id):
        self.introductions.append((platform, platform_user_id, helper_id))
        return f"hi, i'm {helper_id}!"


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
        return LinkOutcome(
            self.result,
            user_uuid=(
                "u1"
                if self.result in (LinkResult.LINKED, LinkResult.RELINKED)
                else None
            ),
        )


def _interface(
    chat=None,
    links=None,
    users=None,
    bot=None,
    *,
    helper_id="chordial",
    deduper=None,
    group_chat_id=None,
    handle_to_helper=None,
):
    iface = TelegramInterface(
        helper_id,
        "123456:TEST-token",
        f"{helper_id}_bot",
        chat or FakeChatService(),
        links if links is not None else FakeLinkService(),
        users or FakeUserManager(),
        deduper if deduper is not None else UpdateDeduper(),
        group_chat_id=group_chat_id,
        handle_to_helper=handle_to_helper,
    )
    iface.app = types.SimpleNamespace(bot=bot or FakeBot())
    return iface


class FakeEntity:
    def __init__(self, type, offset, length, user=None):
        self.type = type
        self.offset = offset
        self.length = length
        self.user = user


class FakeMessage:
    def __init__(self, text, message_id=42, entities=None):
        self.text = text
        self.message_id = message_id
        self.date = None
        self.entities = entities
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeChat:
    def __init__(self, chat_id=None):
        self.id = chat_id
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


def _group_update(
    text, user_id=777, username="wanderer", chat_id=-100, message_id=42, entities=None
):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=user_id, username=username),
        effective_chat=FakeChat(chat_id=chat_id),
        message=FakeMessage(text, message_id=message_id, entities=entities),
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
    assert chat.received == []  # the load-bearing assertion


def test_stranger_code_shaped_text_attempts_redemption():
    links = FakeLinkService(LinkResult.LINKED)
    iface = _interface(links=links)
    update = _update("  abcd2345 ")  # lowercased + padded; normalization's job
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
    assert unified.chat_scope == "dm"
    assert unified.via_bot == "chordial"
    assert unified.dm_helper == "chordial"
    assert update.effective_chat.sent == ["hey dain!"]
    assert update.effective_chat.actions  # typing indicator fired


def test_dm_stamps_the_receiving_bots_helper_id():
    chat = FakeChatService(reply=None)
    iface = _interface(
        chat=chat, users=FakeUserManager(known_ids={"777"}), helper_id="tempo"
    )
    update = _update("push day?")
    run(iface._on_message(update, _ctx()))

    assert len(chat.received) == 1
    unified = chat.received[0]
    assert unified.via_bot == "tempo"
    assert unified.dm_helper == "tempo"


# --- inbound: /start ---------------------------------------------------------------


def test_start_with_payload_redeems():
    links = FakeLinkService(LinkResult.LINKED)
    iface = _interface(links=links)
    update = _update("/start")
    run(iface._on_start(update, _ctx(args=["ABCD2345"])))
    assert links.redeemed[0][0] == "ABCD2345"
    assert update.message.replies == [LINKED_REPLY]


def test_start_meet_known_user_begins_this_helpers_introduction():
    """the meet-the-guides deep link (t.me/<bot>?start=meet): a known user
    taps tempo's link and tempo introduces itself in dm - NOT a code redeem."""
    chat = FakeChatService()
    links = FakeLinkService()
    iface = _interface(
        chat=chat,
        links=links,
        helper_id="tempo",
        users=FakeUserManager(known_ids={"777"}),
    )
    update = _update("/start")
    run(iface._on_start(update, _ctx(args=["meet"])))

    assert chat.introductions == [("telegram", "777", "tempo")]
    assert links.redeemed == []  # never treated as a code
    assert update.effective_chat.sent == ["hi, i'm tempo!"]  # chunked send path


def test_start_meet_stranger_gets_static_reply_and_no_introduction():
    """a stranger can't meet a guide - they must link via chordial first."""
    chat = FakeChatService()
    iface = _interface(chat=chat, users=FakeUserManager(known_ids=set()))
    update = _update("/start")
    run(iface._on_start(update, _ctx(args=["meet"])))

    assert chat.introductions == []
    assert update.message.replies == [STRANGER_REPLY]


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


# --- inbound: group chat --------------------------------------------------------

_HANDLES = {"chordial_bot": "chordial", "tempo_bot": "tempo", "aria_bot": "aria"}


def test_group_known_user_builds_group_unified_and_sends_nothing():
    chat = FakeChatService(reply=None)  # group scope returns None
    iface = _interface(
        chat=chat,
        users=FakeUserManager(known_ids={"777"}),
        helper_id="tempo",
        handle_to_helper=_HANDLES,
        group_chat_id="-100777",
    )
    update = _group_update("hey crew", chat_id=-100777)
    run(iface._on_group_message(update, _ctx()))

    assert len(chat.received) == 1
    unified = chat.received[0]
    assert unified.chat_scope == "group"
    assert unified.group_chat_id == "-100777"
    assert unified.via_bot == "tempo"
    assert unified.mentioned == []
    assert update.effective_chat.sent == []  # delivered out-of-band


def test_group_unknown_sender_is_ignored_silently():
    chat = FakeChatService()
    iface = _interface(
        chat=chat, handle_to_helper=_HANDLES, group_chat_id="-100"
    )  # nobody known
    update = _group_update("who am i")
    run(iface._on_group_message(update, _ctx()))

    assert chat.received == []  # never reaches chat_service
    assert update.effective_chat.sent == []
    assert update.message.replies == []  # no stranger line in a group


def test_group_message_parses_mentions_in_order():
    chat = FakeChatService(reply=None)
    iface = _interface(
        chat=chat,
        users=FakeUserManager(known_ids={"777"}),
        handle_to_helper=_HANDLES,
        group_chat_id="-100",
    )
    text = "@tempo_bot and @aria_bot help"
    entities = [
        FakeEntity("mention", 0, len("@tempo_bot")),
        FakeEntity("mention", 15, len("@aria_bot")),
    ]
    update = _group_update(text, entities=entities)
    run(iface._on_group_message(update, _ctx()))

    assert chat.received[0].mentioned == ["tempo", "aria"]


def test_shared_deduper_processes_a_group_message_once():
    deduper = UpdateDeduper()
    known = FakeUserManager(known_ids={"777"})
    chat_a = FakeChatService(reply=None)
    chat_b = FakeChatService(reply=None)
    # two helper bots share one deduper (as main wires them)
    bot_a = _interface(
        chat=chat_a,
        users=known,
        helper_id="chordial",
        deduper=deduper,
        handle_to_helper=_HANDLES,
        group_chat_id="-100",
    )
    bot_b = _interface(
        chat=chat_b,
        users=known,
        helper_id="tempo",
        deduper=deduper,
        handle_to_helper=_HANDLES,
        group_chat_id="-100",
    )

    upd_a = _group_update("morning!", chat_id=-100, message_id=99)
    upd_b = _group_update("morning!", chat_id=-100, message_id=99)
    run(bot_a._on_group_message(upd_a, _ctx()))
    run(bot_b._on_group_message(upd_b, _ctx()))

    # exactly one bot processed it (the first past the shared deduper)
    assert len(chat_a.received) + len(chat_b.received) == 1


# --- /setup_group -----------------------------------------------------------------


def test_group_message_outside_configured_room_is_ignored():
    chat = FakeChatService(reply=None)
    iface = _interface(
        chat=chat, users=FakeUserManager(known_ids={"777"}), group_chat_id="-100555"
    )
    update = _group_update("private context please", chat_id=-100999)
    run(iface._on_group_message(update, _ctx()))

    assert chat.received == []
    assert update.effective_chat.sent == []


def test_group_message_is_ignored_until_room_is_configured():
    chat = FakeChatService(reply=None)
    iface = _interface(chat=chat, users=FakeUserManager(known_ids={"777"}))
    update = _group_update("private context please", chat_id=-100999)
    run(iface._on_group_message(update, _ctx()))

    assert chat.received == []


def test_setup_group_known_user_replies_with_chat_id_before_configuration():
    iface = _interface(users=FakeUserManager(known_ids={"777"}))
    update = _group_update("/setup_group", chat_id=-100555)
    run(iface._on_setup_group(update, _ctx()))
    assert update.message.replies
    assert "-100555" in update.message.replies[0]


def test_setup_group_unknown_user_is_ignored():
    iface = _interface()
    update = _group_update("/setup_group", chat_id=-100555)
    run(iface._on_setup_group(update, _ctx()))
    assert update.message.replies == []


def test_setup_group_cannot_replace_an_existing_configured_room():
    iface = _interface(
        users=FakeUserManager(known_ids={"777"}), group_chat_id="-100555"
    )
    update = _group_update("/setup_group", chat_id=-100999)
    run(iface._on_setup_group(update, _ctx()))
    assert update.message.replies == []


# --- mention parsing (unit) -------------------------------------------------------


def test_mentioned_helpers_maps_handles_lowercased_and_dedupes():
    msg = FakeMessage(
        "@Tempo_Bot @tempo_bot @aria_bot",
        entities=[
            FakeEntity("mention", 0, len("@Tempo_Bot")),
            FakeEntity("mention", 11, len("@tempo_bot")),
            FakeEntity("mention", 22, len("@aria_bot")),
        ],
    )
    assert mentioned_helpers(msg, _HANDLES) == ["tempo", "aria"]


def test_mentioned_helpers_ignores_unknown_handles_and_no_entities():
    assert mentioned_helpers(FakeMessage("hi", entities=None), _HANDLES) == []
    msg = FakeMessage("@stranger_bot", entities=[FakeEntity("mention", 0, 13)])
    assert mentioned_helpers(msg, _HANDLES) == []


def test_mentioned_helpers_handles_text_mention_entities():
    user = types.SimpleNamespace(username="Aria_Bot")
    msg = FakeMessage("aria", entities=[FakeEntity("text_mention", 0, 4, user=user)])
    assert mentioned_helpers(msg, _HANDLES) == ["aria"]
