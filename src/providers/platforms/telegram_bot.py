"""telegram interface: the second door into the same conversation.

python-telegram-bot v22 in MANUAL mode (initialize/start/start_polling, not
run_polling) - the documented pattern for coexisting in one asyncio loop with
discord.py: no signal handlers installed, polling runs as ptb-managed
background tasks, and start() parks on an event until stop() so main's
create_task(interface.start()) treatment stays uniform across platforms.

identity rules (the public-username safety property):
- a KNOWN linked sender flows into chat_service like any discord dm.
- an UNKNOWN sender never reaches chat_service (which would create a user and
  spend api budget). if their message looks like a link code, we try to
  redeem it; otherwise they get one static line pointing them at the link
  flow. no user row, no onboarding, no model call.

telegram facts this file leans on (verified against current docs):
- a bot cannot message first; /start must happen once - the deep link
  https://t.me/<bot>?start=<code> both satisfies that and delivers the code.
- in private chats chat_id == user_id; we key everything on the numeric id
  (usernames are optional and mutable).
- blocked/stopped -> 403 Forbidden (permanent); "chat not found" BadRequest
  -> never /start-ed (permanent); RetryAfter carries a wait time; 4096-char
  message cap -> chunk; ≤1 msg/sec per chat -> pause between chunks.
"""

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Optional

from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .base import BaseInterface, UndeliverableError
from config import Config
from src.services.platform_link_service import LinkResult
from src.utils.string_utils import chunk_message

logger = logging.getLogger(__name__)


class UpdateDeduper:
    """N bots in one group each deliver every human message; keep the first.

    every helper bot polls its own update stream, so a single human line in the
    shared group arrives once per bot - all with the same (chat_id, message_id).
    all interfaces share ONE deduper instance; the first bot to observe a given
    (chat_id, message_id) processes it, the rest see a duplicate and drop it.
    """

    def __init__(self, maxlen: int = 512):
        self._seen: "OrderedDict[tuple[int, int], None]" = OrderedDict()
        self._maxlen = maxlen

    def is_duplicate(self, chat_id: int, message_id: int) -> bool:
        key = (chat_id, message_id)
        if key in self._seen:
            return True
        self._seen[key] = None
        if len(self._seen) > self._maxlen:
            self._seen.popitem(last=False)
        return False


def mentioned_helpers(message, handle_to_helper: "dict[str, str] | None") -> list[str]:
    """resolve @-mentions in a group message to helper ids, in order (deduped).

    telegram hands us `MessageEntity` spans instead of leaving us to regex the
    text: a "mention" entity is an '@handle' substring we slice out of the text
    and map to a helper id; a "text_mention" entity carries a `User` object
    directly (used when a bot has no public username) whose `.username` we map
    the same way. unknown handles are ignored.
    """
    handle_to_helper = handle_to_helper or {}
    out: list[str] = []
    text = message.text or ""
    for ent in message.entities or []:
        helper: Optional[str] = None
        if ent.type == "mention":
            handle = text[ent.offset + 1 : ent.offset + ent.length]  # drop the '@'
            helper = handle_to_helper.get(handle.lower())
        elif ent.type == "text_mention" and getattr(ent, "user", None) is not None:
            username = getattr(ent.user, "username", None)
            if username:
                helper = handle_to_helper.get(username.lower())
        if helper and helper not in out:
            out.append(helper)
    return out


_TELEGRAM_MAX_LENGTH = 4096
_INTER_CHUNK_DELAY = 1.0  # stay under telegram's ~1 msg/sec per chat
_LINK_CODE_RE = re.compile(r"^[A-Z2-9]{8}$")
# the /start payload chordial's meet-the-guides deep links carry
# (t.me/<bot>?start=<this>). MUST match the value intro_tools._deep_link builds.
_MEET_PAYLOAD = "meet"

STRANGER_REPLY = (
    "hi! i'm chordial — a personal companion, so i only chat with people i "
    "already know. if that's you, ask me for a link code on our usual "
    "platform and send it here 💜"
)
ALREADY_LINKED_REPLY = "hey, you're already linked here 💜 say anything!"
LINKED_REPLY = (
    "hey, it's really you! 💜 we're connected now — this and our other chats "
    "are one conversation. say anything ✨"
)
RELINKED_REPLY = "welcome back! 💜 this chat is connected again — say anything ✨"
INVALID_CODE_REPLY = (
    "that code doesn't look right — ask me for a fresh one on your usual " "platform 💜"
)
EXPIRED_CODE_REPLY = (
    "that code's expired (they only last a few minutes) — grab a fresh one "
    "and try again ⏰"
)
CONFLICT_REPLY = (
    "hmm, this telegram account is already connected to someone. if that "
    "seems wrong, let's sort it out on your main platform 💜"
)


class TelegramInterface(BaseInterface):
    """one helper's telegram bot (long polling): its DMs + the shared group.

    in v3 each helper (chordial, tempo, ...) runs as its own bot account with
    its own token, so main builds one interface per enabled helper. they all
    still report `platform == "telegram"`; the router disambiguates by
    `helper_id`. per-helper state carried here:
    - `helper_id`: which persona this bot speaks as (event-log author / router
      sub-key / the `via_bot` stamp on inbound messages).
    - `deduper`: a SHARED `UpdateDeduper` across every interface - N bots each
      receive every human group message, and only the first delivery of a given
      (chat_id, message_id) is processed.
    - `handle_to_helper`: bot @handle (lowercase, no '@') -> helper id, for
      resolving @mentions in group messages to the helpers they summon.
    """

    platform = "telegram"

    def __init__(
        self,
        helper_id: str,
        token: str,
        telegram_handle: str,
        chat_service,
        link_service,
        user_manager,
        deduper,
        group_chat_id=None,
        handle_to_helper: "dict[str, str] | None" = None,
    ):
        super().__init__(chat_service)
        self.helper_id = helper_id
        self.token = token
        self.telegram_handle = telegram_handle
        self.link_service = link_service
        self.user_manager = user_manager
        self.deduper = deduper
        self.group_chat_id = group_chat_id
        self.handle_to_helper = handle_to_helper or {}
        self._stopped = asyncio.Event()

        self.app: Application = ApplicationBuilder().token(token).build()
        self.app.add_handler(CommandHandler("start", self._on_start))
        self.app.add_handler(
            CommandHandler(
                "setup_group",
                self._on_setup_group,
                filters=filters.ChatType.GROUPS,
            )
        )
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
                self._on_message,
            )
        )
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
                self._on_group_message,
            )
        )

    # --- lifecycle -----------------------------------------------------------

    async def start(self):
        """start polling, then park until stop() - keeps the BaseInterface
        contract uniform with discord (start() blocks for the app's life).
        a startup failure logs loudly instead of silently killing the gather."""
        try:
            await self.app.initialize()

            # nice-to-have sanity check: the configured @username (deep links,
            # mention parsing - see Config.telegram_username_for) should
            # actually belong to this token's bot. every helper's own handle
            # is checked, not just chordial's - a mismatch here means deep
            # links/mentions for THIS helper are silently pointing at the
            # wrong bot (most likely: BotFather username != TELEGRAM_USERNAME_*).
            me = await self.app.bot.get_me()
            if self.telegram_handle and me.username != self.telegram_handle:
                logger.warning(
                    "configured username for '%s' is %r but the token belongs "
                    "to @%s - deep links/mentions for this helper will point "
                    "at the wrong bot! fix TELEGRAM_USERNAME_%s (or "
                    "TELEGRAM_BOT_USERNAME for chordial).",
                    self.helper_id,
                    self.telegram_handle,
                    me.username,
                    self.helper_id.upper(),
                )

            await self.app.start()
            await self.app.updater.start_polling(
                timeout=30,  # long-poll hold, not sleep-polling
                drop_pending_updates=True,  # don't replay a backlog after downtime
            )
            logger.info("telegram interface polling as @%s", me.username)
        except Exception:
            logger.exception("telegram interface failed to start")
            return
        await self._stopped.wait()

    async def stop(self):
        """ptb's documented manual shutdown sequence, then release start()."""
        try:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception:
            logger.exception("error stopping telegram interface")
        finally:
            self._stopped.set()

    # --- inbound ---------------------------------------------------------------

    async def _on_start(self, update, context: ContextTypes.DEFAULT_TYPE):
        """/start - carries one of two deep-link payloads, or none:
        - `meet`: the meet-the-guides link (t.me/<bot>?start=meet) that chordial
          hands out. a KNOWN user tapping it kicks off THIS helper's own
          introduction in dm. (a stranger can't meet a guide - they link via
          chordial first, so they fall through to the stranger line.)
        - anything else: treated as a cross-platform link CODE to redeem (the
          under-the-hood account-linking flow; the user never types these).
        """
        user = update.effective_user
        if user is None or update.message is None:
            return

        payload = context.args[0] if context.args else None

        if payload == _MEET_PAYLOAD:
            if await self._is_known(str(user.id)):
                await update.effective_chat.send_action(ChatAction.TYPING)
                reply = await self.chat_service.begin_introduction(
                    "telegram",
                    str(user.id),
                    self.helper_id,
                )
                await self._send_chunked(update, reply)
            else:
                await update.message.reply_text(STRANGER_REPLY)
            return

        if payload:
            await update.message.reply_text(await self._redeem(payload, user))
            return

        if await self._is_known(str(user.id)):
            await update.message.reply_text(ALREADY_LINKED_REPLY)
        else:
            await update.message.reply_text(STRANGER_REPLY)

    async def _send_chunked(self, update, text) -> None:
        """send a (possibly long) reply as telegram-sized chunks, paced under
        the per-chat rate limit - the same shape _on_message uses."""
        if not text:
            return
        chunks = chunk_message(text, max_length=_TELEGRAM_MAX_LENGTH)
        for i, chunk in enumerate(chunks):
            await update.effective_chat.send_message(chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(_INTER_CHUNK_DELAY)

    async def _on_message(self, update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message = update.message
        if user is None or message is None or not message.text:
            return

        if not await self._is_known(str(user.id)):
            # unknown stranger: NEVER reaches chat_service. a code-shaped
            # message gets a redemption attempt; anything else, one static line.
            candidate = message.text.strip().upper()
            if _LINK_CODE_RE.fullmatch(candidate):
                await message.reply_text(await self._redeem(candidate, user))
            else:
                await message.reply_text(STRANGER_REPLY)
            return

        from src.models.unified_message import UnifiedMessage

        unified_msg = UnifiedMessage(
            content=message.text,
            platform_user_id=str(user.id),
            platform="telegram",
            platform_message_id=str(message.message_id),
            chat_scope="dm",
            via_bot=self.helper_id,
            dm_helper=self.helper_id,
            metadata={
                "username": user.username,
                "timestamp": message.date,
            },
        )

        await update.effective_chat.send_action(ChatAction.TYPING)
        response = await self.chat_service.process_message(unified_msg)

        if response:
            chunks = chunk_message(response, max_length=_TELEGRAM_MAX_LENGTH)
            for i, chunk in enumerate(chunks):
                await update.effective_chat.send_message(
                    chunk
                )  # plain text, no parse_mode
                if i < len(chunks) - 1:
                    await asyncio.sleep(_INTER_CHUNK_DELAY)

    async def _on_group_message(self, update, context: ContextTypes.DEFAULT_TYPE):
        """a human line in the shared crew group. delivered to every bot, so we
        dedupe first; unknown senders are ignored (no user row, no reply); the
        reply is delivered out-of-band by the orchestrator/router, so we send
        nothing back here (chat_service returns None for group scope)."""
        user = update.effective_user
        message = update.message
        chat = update.effective_chat
        if user is None or message is None or not message.text or chat is None:
            return

        # The crew room contains personal context, so knowing the sender is not
        # sufficient authorization: a known user may add a helper bot to any
        # number of unrelated groups.  Fail closed until a room is configured,
        # and only accept updates from that exact Telegram chat.
        if not self._is_configured_group(chat.id):
            logger.warning(
                "ignoring group message for helper '%s' from unconfigured "
                "telegram chat %s",
                self.helper_id,
                chat.id,
            )
            return

        # N bots => N identical updates; only the first past the shared deduper
        # is processed. keyed on (chat_id, message_id), stable across streams.
        if self.deduper is not None and self.deduper.is_duplicate(
            chat.id, message.message_id
        ):
            return

        # unknown senders in a group are ignored silently: no onboarding, no
        # stranger line, no api spend (the group is a known-users-only room).
        if not await self._is_known(str(user.id)):
            return

        mentioned = mentioned_helpers(message, self.handle_to_helper)

        from src.models.unified_message import UnifiedMessage

        unified_msg = UnifiedMessage(
            content=message.text,
            platform_user_id=str(user.id),
            platform="telegram",
            platform_message_id=str(message.message_id),
            chat_scope="group",
            group_chat_id=str(chat.id),
            via_bot=self.helper_id,
            mentioned=mentioned,
            metadata={
                "username": user.username,
                "timestamp": message.date,
            },
        )

        # group scope: the orchestrator delivers each speaker's line out-of-band
        # via the router, so process_message returns None and we send nothing.
        # (a returned string is an acceptable fallback, but the contract is None.)
        response = await self.chat_service.process_message(unified_msg)
        if response:
            chunks = chunk_message(response, max_length=_TELEGRAM_MAX_LENGTH)
            for i, chunk in enumerate(chunks):
                await chat.send_message(chunk)
                if i < len(chunks) - 1:
                    await asyncio.sleep(_INTER_CHUNK_DELAY)

    async def _on_setup_group(self, update, context: ContextTypes.DEFAULT_TYPE):
        """/setup_group in a group: a discovery helper. reply with the chat id
        and how to persist it (set TELEGRAM_GROUP_CHAT_ID) - phase-2 keeps this
        lightweight, since Dain creates the group manually and sets the env."""
        user = update.effective_user
        message = update.message
        chat = update.effective_chat
        if user is None or message is None or chat is None:
            return

        # Only an already-linked account may discover/configure the private
        # crew room. Once a room is configured, do not disclose or suggest a
        # replacement id from commands issued in another group.
        if not await self._is_known(str(user.id)):
            return
        if self.group_chat_id is not None and not self._is_configured_group(chat.id):
            return
        # keep it deduped too: every bot in the group sees this command.
        if self.deduper is not None and self.deduper.is_duplicate(
            chat.id, message.message_id
        ):
            return
        await message.reply_text(
            f"this group's chat id is `{chat.id}`.\n"
            f"set TELEGRAM_GROUP_CHAT_ID={chat.id} in the environment and "
            f"restart so the crew can speak here."
        )

    def _is_configured_group(self, chat_id) -> bool:
        """Whether ``chat_id`` is the explicitly configured private crew room."""
        return self.group_chat_id is not None and str(chat_id) == str(
            self.group_chat_id
        )

    async def _is_known(self, platform_user_id: str) -> bool:
        return not await self.user_manager.is_new_user("telegram", platform_user_id)

    async def _redeem(self, code: str, user) -> str:
        """redeem a link code for the sending telegram account -> reply text."""
        if self.link_service is None:
            return STRANGER_REPLY
        outcome = await self.link_service.redeem(
            code,
            "telegram",
            str(user.id),
            user.username,
        )
        return {
            LinkResult.LINKED: LINKED_REPLY,
            LinkResult.RELINKED: RELINKED_REPLY,
            LinkResult.INVALID: INVALID_CODE_REPLY,
            LinkResult.EXPIRED: EXPIRED_CODE_REPLY,
            LinkResult.CONFLICT: CONFLICT_REPLY,
        }[outcome.result]

    # --- outbound ----------------------------------------------------------------

    async def send_message(self, platform_user_id: str, content: str, **kwargs) -> bool:
        """send a message to a telegram user, chunked.

        raises UndeliverableError for permanent failures (blocked the bot,
        never /start-ed, malformed id) so the router deactivates the link;
        transient failures return False and leave the link active."""
        try:
            chat_id = int(platform_user_id)
            chunks = chunk_message(content, max_length=_TELEGRAM_MAX_LENGTH)

            await self.app.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.TYPING
            )
            for i, chunk in enumerate(chunks):
                await self._send_chunk(chat_id, chunk)
                if i < len(chunks) - 1:
                    await asyncio.sleep(_INTER_CHUNK_DELAY)

            logger.info(
                f"sent telegram message to {platform_user_id} "
                f"({len(chunks)} chunk{'s' if len(chunks) > 1 else ''})"
            )
            return True

        except Forbidden as e:
            # 403 - user blocked/stopped the bot; won't succeed until they /start again
            raise UndeliverableError(
                f"telegram user {platform_user_id} blocked the bot"
            ) from e
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                # they never /start-ed this bot (or the id is dead)
                raise UndeliverableError(
                    f"telegram chat {platform_user_id} not found (never started?)"
                ) from e
            logger.error(f"telegram bad request sending to {platform_user_id}: {e}")
            return False
        except ValueError as e:
            # non-integer platform_user_id - malformed link, never deliverable
            raise UndeliverableError(
                f"invalid telegram user id '{platform_user_id}'"
            ) from e
        except TelegramError as e:
            # transient (network, 5xx, a RetryAfter retry that failed again...)
            logger.error(f"transient telegram error sending to {platform_user_id}: {e}")
            return False

    async def _send_chunk(self, chat_id: int, chunk: str) -> None:
        """one sendMessage with a single honored retry on rate limiting."""
        try:
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)
        except RetryAfter as e:
            wait = e.retry_after
            seconds = (
                wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            )
            logger.warning(f"telegram rate limit, retrying in {seconds:.1f}s")
            await asyncio.sleep(seconds + 0.5)
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)

    async def handle_incoming_message(self, message):
        """inbound is wired through ptb handlers (_on_message); nothing routes
        through this BaseInterface hook for telegram."""
        raise NotImplementedError("telegram inbound flows through ptb handlers")
