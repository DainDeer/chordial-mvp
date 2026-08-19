"""tether interface tests (ROOMS_DESIGN section 9): handler policy, inline
attribution, the desktop mirror, and outbound error mapping, with ptb's
Application/bot faked (no network, no token).

the properties that matter:
- unknown senders NEVER reach chat_service (no user creation, no api spend):
  code-shaped text attempts redemption, everything else gets one static line
- a known sender's message is a GROUP-scope turn into today's daily room
  (chat_id == user_id in private chats, so the group target is the sender),
  and their line mirrors best-effort to connected desktop surfaces
- /start with a deep-link payload redeems; bare /start is polite; the
  ensemble-era 'meet' payload is just an unrecognizable code now
- outbound renders inline attribution (one bot, whole council) and maps
  telegram errors onto the router's contract: Forbidden and chat-not-found
  -> UndeliverableError (permanent), other TelegramError -> False
  (transient), RetryAfter honored once
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

from src.personas import load_personas  # noqa: E402
from src.providers.platforms.telegram_bot import (  # noqa: E402
    TelegramInterface,
    attributed,
    STRANGER_REPLY,
    LINKED_REPLY,
    INVALID_CODE_REPLY,
    ALREADY_LINKED_REPLY,
    OPEN_ONBOARDING_HELLO,
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
        self.markups = []  # reply_markup objects, when passed
        self.actions = []  # chat actions
        self.send_error = send_error
        self._retry_once = retry_once

    async def send_message(self, chat_id, text, reply_markup=None):
        if self._retry_once:
            self._retry_once = False
            raise RetryAfter(0)  # 0-second wait keeps the test fast
        if self.send_error:
            raise self.send_error
        self.sent.append((chat_id, text))
        if reply_markup is not None:
            self.markups.append(reply_markup)

    async def send_chat_action(self, chat_id, action):
        if self.send_error and isinstance(self.send_error, (Forbidden, BadRequest)):
            raise self.send_error
        self.actions.append((chat_id, action))


class FakeChatService:
    def __init__(self, reply=None):
        # group scope's contract is None (delivered out-of-band); a string
        # models refusal/error copy the interface must still send
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

    async def lookup_user_uuid(self, platform, platform_user_id):
        return f"uuid-{platform_user_id}" if platform_user_id in self.known else None


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


class FakeMirror:
    """the app interface's fan-out, as the tether sees it."""

    def __init__(self, fail=False):
        self.fanned = []  # (user_uuid, content, kwargs)
        self.fail = fail

    async def send_message(self, platform_user_id, content, **kwargs):
        if self.fail:
            raise RuntimeError("mirror cracked")
        self.fanned.append((platform_user_id, content, kwargs))
        return True


def _interface(chat=None, links=None, users=None, bot=None, mirror=None):
    iface = TelegramInterface(
        token="123456:TEST-token",
        telegram_handle="chordial_bot",
        chat_service=chat or FakeChatService(),
        link_service=links if links is not None else FakeLinkService(),
        user_manager=users or FakeUserManager(),
        mirror=mirror,
    )
    iface.app = types.SimpleNamespace(bot=bot or FakeBot())
    return iface


class FakeMessage:
    def __init__(self, text, message_id=42):
        self.text = text
        self.message_id = message_id
        self.date = None
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
        effective_chat=FakeChat(chat_id=user_id),
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


# --- inbound: open onboarding (TELEGRAM_OPEN_ONBOARDING) ---------------------------


def test_open_onboarding_stranger_reaches_chat_service(monkeypatch):
    """with the flag on, a stranger's message flows into chat_service exactly
    like a first app message - user creation + introduction are its job."""
    monkeypatch.setattr(Config, "TELEGRAM_OPEN_ONBOARDING", True)
    chat = FakeChatService(reply="welcome to the forest!")
    iface = _interface(chat=chat)
    update = _update("hello?? who are you")
    run(iface._on_message(update, _ctx()))

    assert len(chat.received) == 1
    assert chat.received[0].content == "hello?? who are you"
    assert update.message.replies == []           # no stranger wall
    assert update.effective_chat.sent == ["welcome to the forest!"]


def test_open_onboarding_code_shaped_text_still_redeems(monkeypatch):
    """linking beats onboarding: a code-shaped message from a stranger is a
    redemption attempt even when onboarding is open (never a model call)."""
    monkeypatch.setattr(Config, "TELEGRAM_OPEN_ONBOARDING", True)
    chat = FakeChatService()
    links = FakeLinkService(LinkResult.LINKED)
    iface = _interface(chat=chat, links=links)
    update = _update("ABCD2345")
    run(iface._on_message(update, _ctx()))

    assert links.redeemed and chat.received == []


def test_open_onboarding_bare_start_gets_hello_not_stranger_wall(monkeypatch):
    monkeypatch.setattr(Config, "TELEGRAM_OPEN_ONBOARDING", True)
    chat = FakeChatService()
    iface = _interface(chat=chat)
    update = _update("/start")
    run(iface._on_start(update, _ctx()))

    assert update.message.replies == [OPEN_ONBOARDING_HELLO]
    assert chat.received == []                    # static line, no model call


# --- inbound: the tether is a group-scope turn into today's room -------------------


def test_known_user_speaks_into_the_daily_room_as_group_scope():
    """the tether maps to the council's shared room: group scope (the
    routing spine picks the speaker), the group target is the private chat
    itself, and the reply arrives out-of-band - nothing sent here."""
    chat = FakeChatService(reply=None)
    iface = _interface(chat=chat, users=FakeUserManager(known_ids={"777"}))
    update = _update("good morning!")
    run(iface._on_message(update, _ctx()))

    assert len(chat.received) == 1
    unified = chat.received[0]
    assert unified.platform == "telegram"
    assert unified.platform_user_id == "777"
    assert unified.chat_scope == "group"
    assert unified.group_chat_id == "777"        # chat_id == user_id in dms
    assert update.effective_chat.sent == []      # delivered via the router
    assert update.effective_chat.actions         # typing indicator fired


def test_refusal_copy_still_sends_in_place():
    """a returned string is refusal/error copy (never persisted) - the one
    case the interface still answers directly."""
    chat = FakeChatService(reply="i don't think i can help with that one")
    iface = _interface(chat=chat, users=FakeUserManager(known_ids={"777"}))
    update = _update("do the impossible")
    run(iface._on_message(update, _ctx()))

    assert update.effective_chat.sent == ["i don't think i can help with that one"]


# --- inbound: the desktop mirror (one conversation, two windows) -------------------


def test_known_users_line_mirrors_to_the_desktop():
    mirror = FakeMirror()
    chat = FakeChatService(reply=None)
    iface = _interface(chat=chat, users=FakeUserManager(known_ids={"777"}),
                       mirror=mirror)
    update = _update("logging lunch from the bus")
    run(iface._on_message(update, _ctx()))

    assert len(mirror.fanned) == 1
    user_uuid, content, kwargs = mirror.fanned[0]
    assert user_uuid == "uuid-777"
    assert content == "logging lunch from the bus"
    assert kwargs["author_type"] == "user"
    assert kwargs["source_platform"] == "telegram"
    assert len(chat.received) == 1               # the turn still ran


def test_mirror_failure_never_costs_the_turn():
    mirror = FakeMirror(fail=True)
    chat = FakeChatService(reply=None)
    iface = _interface(chat=chat, users=FakeUserManager(known_ids={"777"}),
                       mirror=mirror)
    update = _update("still here?")
    run(iface._on_message(update, _ctx()))

    assert len(chat.received) == 1               # the mirror is cosmetic


def test_stranger_without_user_row_is_never_mirrored(monkeypatch):
    """open onboarding's first contact has no user row yet: the mirror
    no-ops instead of inventing one (lookup is read-only by contract)."""
    monkeypatch.setattr(Config, "TELEGRAM_OPEN_ONBOARDING", True)
    mirror = FakeMirror()
    chat = FakeChatService(reply="hello, new friend")
    iface = _interface(chat=chat, users=FakeUserManager(), mirror=mirror)
    update = _update("hi, i'm new")
    run(iface._on_message(update, _ctx()))

    assert mirror.fanned == []
    assert len(chat.received) == 1


# --- inbound: /start ---------------------------------------------------------------


def test_start_with_payload_redeems():
    links = FakeLinkService(LinkResult.LINKED)
    iface = _interface(links=links)
    update = _update("/start")
    run(iface._on_start(update, _ctx(args=["ABCD2345"])))
    assert links.redeemed[0][0] == "ABCD2345"
    assert update.message.replies == [LINKED_REPLY]


def test_legacy_meet_payload_is_just_an_unrecognized_code():
    """the ensemble-era meet-the-guides deep link: whatever payload arrives
    is a redemption attempt now - no introduction machinery, no model call."""
    links = FakeLinkService(LinkResult.INVALID)
    chat = FakeChatService()
    iface = _interface(chat=chat, links=links,
                       users=FakeUserManager(known_ids={"777"}))
    update = _update("/start")
    run(iface._on_start(update, _ctx(args=["meet"])))

    assert links.redeemed[0][0] == "meet"
    assert update.message.replies == [INVALID_CODE_REPLY]
    assert chat.received == []


def test_bare_start_stranger_vs_known():
    iface = _interface()
    update = _update("/start")
    run(iface._on_start(update, _ctx()))
    assert update.message.replies == [STRANGER_REPLY]

    iface = _interface(users=FakeUserManager(known_ids={"777"}))
    update = _update("/start")
    run(iface._on_start(update, _ctx()))
    assert update.message.replies == [ALREADY_LINKED_REPLY]


# --- outbound: inline attribution --------------------------------------------------


def _a_council_member():
    """a real deployed card with its emoji - the attribution's raw material."""
    cards = load_personas()
    helper_id = sorted(set(Config.ENABLED_HELPERS) & set(cards))[0]
    return helper_id, cards[helper_id]


def test_attributed_renders_emoji_speaker_and_words():
    helper_id, card = _a_council_member()
    line = attributed(helper_id, "burrito logged, ~650")
    assert line == f"{card.emoji} {helper_id} — burrito logged, ~650"


def test_attributed_passes_through_unknown_or_absent_speakers():
    assert attributed(None, "just words") == "just words"
    assert attributed("nobody-of-that-name", "just words") == "just words"


def test_send_message_attributes_the_speaker_inline():
    helper_id, card = _a_council_member()
    bot = FakeBot()
    iface = _interface(bot=bot)
    ok = run(iface.send_message("777", "hello from the council",
                                speaker=helper_id, stream_id="room-1"))
    assert ok is True
    assert bot.sent == [(777, f"{card.emoji} {helper_id} — hello from the council")]


def test_send_message_without_speaker_sends_plain_text():
    bot = FakeBot()
    iface = _interface(bot=bot)
    ok = run(iface.send_message("777", "system words"))
    assert ok is True
    assert bot.sent == [(777, "system words")]


def test_send_choice_attributes_the_ping():
    helper_id, card = _a_council_member()
    bot = FakeBot()
    iface = _interface(bot=bot)
    ok = run(iface.send_choice(
        "777", "want that time back?",
        [{"label": "yes", "data": "rw:yes"}], speaker=helper_id))
    assert ok is True
    assert bot.sent == [(777, f"{card.emoji} {helper_id} — want that time back?")]
    assert bot.markups  # the buttons rode the same message


# --- outbound: chunking + error mapping --------------------------------------------


def test_send_message_chunks_and_paces():
    bot = FakeBot()
    iface = _interface(bot=bot)
    long = ("x" * 3000) + "\n\n" + ("y" * 3000)
    ok = run(iface.send_message("777", long))
    assert ok is True
    assert len(bot.sent) == 2
    assert all(len(text) <= _TELEGRAM_MAX_LENGTH for _, text in bot.sent)
    assert bot.actions  # typing indicator before the sends


def test_attribution_counts_toward_the_chunk_limit():
    """the rendered prefix is part of the message: chunking happens AFTER
    attribution, so a line near the cap still fits every chunk."""
    helper_id, _card = _a_council_member()
    bot = FakeBot()
    iface = _interface(bot=bot)
    ok = run(iface.send_message("777", "z" * _TELEGRAM_MAX_LENGTH,
                                speaker=helper_id))
    assert ok is True
    assert len(bot.sent) == 2  # the prefix pushed it over one message
    assert all(len(text) <= _TELEGRAM_MAX_LENGTH for _, text in bot.sent)


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
