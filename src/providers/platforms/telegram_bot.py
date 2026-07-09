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
from typing import Optional

from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from .base import BaseInterface, UndeliverableError
from config import Config
from src.services.platform_link_service import LinkResult
from src.utils.string_utils import chunk_message

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LENGTH = 4096
_INTER_CHUNK_DELAY = 1.0   # stay under telegram's ~1 msg/sec per chat
_LINK_CODE_RE = re.compile(r"^[A-Z2-9]{8}$")

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
    "that code doesn't look right — ask me for a fresh one on your usual "
    "platform 💜"
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
    """telegram bot implementation (long polling, DMs only)."""

    platform = "telegram"

    def __init__(self, chat_service, link_service, user_manager):
        super().__init__(chat_service)
        self.link_service = link_service
        self.user_manager = user_manager
        self._stopped = asyncio.Event()

        self.app: Application = ApplicationBuilder().token(Config.TELEGRAM_TOKEN).build()
        self.app.add_handler(CommandHandler("start", self._on_start))
        self.app.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            self._on_message,
        ))

    # --- lifecycle -----------------------------------------------------------

    async def start(self):
        """start polling, then park until stop() - keeps the BaseInterface
        contract uniform with discord (start() blocks for the app's life).
        a startup failure logs loudly instead of silently killing the gather."""
        try:
            await self.app.initialize()

            # nice-to-have sanity check: the configured deep-link username
            # should belong to this token's bot
            me = await self.app.bot.get_me()
            if Config.TELEGRAM_BOT_USERNAME and me.username != Config.TELEGRAM_BOT_USERNAME:
                logger.warning(
                    "TELEGRAM_BOT_USERNAME=%r but the token belongs to @%s - "
                    "link-code deep links will point at the wrong bot!",
                    Config.TELEGRAM_BOT_USERNAME, me.username,
                )

            await self.app.start()
            await self.app.updater.start_polling(
                timeout=30,                 # long-poll hold, not sleep-polling
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
        """/start - possibly carrying a link-code payload from a deep link."""
        user = update.effective_user
        if user is None or update.message is None:
            return

        payload = context.args[0] if context.args else None
        if payload:
            reply = await self._redeem(payload, user)
            await update.message.reply_text(reply)
            return

        if await self._is_known(str(user.id)):
            await update.message.reply_text(ALREADY_LINKED_REPLY)
        else:
            await update.message.reply_text(STRANGER_REPLY)

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
                await update.effective_chat.send_message(chunk)  # plain text, no parse_mode
                if i < len(chunks) - 1:
                    await asyncio.sleep(_INTER_CHUNK_DELAY)

    async def _is_known(self, platform_user_id: str) -> bool:
        return not await self.user_manager.is_new_user("telegram", platform_user_id)

    async def _redeem(self, code: str, user) -> str:
        """redeem a link code for the sending telegram account -> reply text."""
        if self.link_service is None:
            return STRANGER_REPLY
        outcome = await self.link_service.redeem(
            code, "telegram", str(user.id), user.username,
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

            await self.app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
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
            raise UndeliverableError(f"invalid telegram user id '{platform_user_id}'") from e
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
            seconds = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            logger.warning(f"telegram rate limit, retrying in {seconds:.1f}s")
            await asyncio.sleep(seconds + 0.5)
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)

    async def handle_incoming_message(self, message):
        """inbound is wired through ptb handlers (_on_message); nothing routes
        through this BaseInterface hook for telegram."""
        raise NotImplementedError("telegram inbound flows through ptb handlers")
