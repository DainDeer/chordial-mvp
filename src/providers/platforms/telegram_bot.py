"""the tether: a single-bot telegram window into today's daily room
(docs/ROOMS_DESIGN.md section 9).

ONE bot carries the whole council. a known sender's message is a group-scope
turn in today's daily room - the same stream the desktop shows - and every
council line delivered back is attributed inline in the text ("🦝 remy — ..."),
because telegram has no in-band author channel the way the app's payload does.
the phase-3 one-bot-per-helper ensemble (per-helper tokens, the shared crew
group, @-mention parsing, the update deduper, meet-the-guides deep links) was
retired outright in phase 7a, not ported: it could never scale past a handful
of users, and the rooms model doesn't need it.

python-telegram-bot v22 in MANUAL mode (initialize/start/start_polling, not
run_polling) - the documented pattern for coexisting in one asyncio loop with
discord.py: no signal handlers installed, polling runs as ptb-managed
background tasks, and start() parks on an event until stop() so main's
create_task(interface.start()) treatment stays uniform across platforms.

identity rules (the public-username safety property):
- a KNOWN linked sender flows into chat_service like any other inbound turn.
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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .base import BaseInterface, UndeliverableError
from config import Config
from src.services import rewind_tether
from src.services.platform_link_service import LinkResult
from src.utils.string_utils import chunk_message

logger = logging.getLogger(__name__)


_TELEGRAM_MAX_LENGTH = 4096
_INTER_CHUNK_DELAY = 1.0  # stay under telegram's ~1 msg/sec per chat
_LINK_CODE_RE = re.compile(r"^[A-Z2-9]{8}$")

# the bare-/start greeting when TELEGRAM_OPEN_ONBOARDING is on: static (no
# model call) - their first real message is what creates the user
OPEN_ONBOARDING_HELLO = (
    "hi! we haven't met yet 💜 say anything and we'll get started."
)

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


def attributed(speaker: Optional[str], content: str) -> str:
    """render a council line for a surface with no author channel: the
    speaker's emoji + id, inline, ahead of the words - "🦝 remy — burrito
    logged". deterministic and total: an unknown or absent speaker id (a
    static system line, a speaker whose card isn't deployed) passes the
    content through untouched rather than inventing an author."""
    if not speaker:
        return content
    from src.personas import load_personas

    card = load_personas().get(speaker)
    if card is None:
        return content
    return f"{card.emoji} {speaker} — {content}"


class TelegramInterface(BaseInterface):
    """the deployment's one telegram bot: every user's tether into their
    daily room. registers with the router as (telegram, None) - the
    single-bot key - so it serves ANY speaker; attribution is rendered into
    the text at send time. `mirror` (optional) is the app interface's
    fan-out, so a known sender's own line also appears live on their
    connected desktop (one conversation, two windows)."""

    platform = "telegram"

    def __init__(
        self,
        token: str,
        telegram_handle: str,
        chat_service,
        link_service,
        user_manager,
        mirror=None,
    ):
        super().__init__(chat_service)
        self.token = token
        self.telegram_handle = telegram_handle
        self.link_service = link_service
        self.user_manager = user_manager
        self.mirror = mirror
        self._stopped = asyncio.Event()

        self.app: Application = ApplicationBuilder().token(token).build()
        self.app.add_handler(CommandHandler("start", self._on_start))
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
                self._on_message,
            )
        )
        # the rewind tether's inline answers (REWIND_DESIGN section 8):
        # whichever ping the user taps, the answer lands here.
        self.app.add_handler(
            CallbackQueryHandler(self._on_rewind_choice, pattern=r"^rw:")
        )

    # --- lifecycle -----------------------------------------------------------

    async def start(self):
        """start polling, then park until stop() - keeps the BaseInterface
        contract uniform with discord (start() blocks for the app's life).
        a startup failure logs loudly instead of silently killing the gather."""
        try:
            await self.app.initialize()

            # nice-to-have sanity check: the configured @username (link-code
            # deep links) should actually belong to this token's bot - a
            # mismatch means minted links silently point at the wrong bot
            # (most likely: BotFather username != TELEGRAM_BOT_USERNAME).
            me = await self.app.bot.get_me()
            if self.telegram_handle and me.username != self.telegram_handle:
                logger.warning(
                    "configured TELEGRAM_BOT_USERNAME is %r but the token "
                    "belongs to @%s - minted deep links will point at the "
                    "wrong bot!",
                    self.telegram_handle,
                    me.username,
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
        """/start - carries a cross-platform link CODE as its deep-link
        payload (the account-linking flow; the user never types these), or
        nothing. (the ensemble-era 'meet' payload is gone with the per-guide
        bots: guides are met in the rooms now.)"""
        user = update.effective_user
        if user is None or update.message is None:
            return

        payload = context.args[0] if context.args else None
        if payload:
            await update.message.reply_text(await self._redeem(payload, user))
            return

        if await self._is_known(str(user.id)):
            await update.message.reply_text(ALREADY_LINKED_REPLY)
        else:
            await update.message.reply_text(self._stranger_start_reply())

    @staticmethod
    def _stranger_start_reply() -> str:
        """what an unknown user's bare /start gets. /start carries no user
        text worth handing the model, so even with open onboarding it stays a
        static line - the invitation to say something IS the onboarding door
        (their first real message creates the user and starts the intro)."""
        if Config.TELEGRAM_OPEN_ONBOARDING:
            return OPEN_ONBOARDING_HELLO
        return STRANGER_REPLY

    async def _on_message(self, update, context: ContextTypes.DEFAULT_TYPE):
        """a private-chat line. known senders speak into today's daily room
        (group scope: the council's routing spine picks who answers, and the
        replies are delivered out-of-band through the router - process_message
        returns None on that path, so anything it DOES return is refusal/error
        copy to send plainly). unknown senders never reach chat_service."""
        user = update.effective_user
        message = update.message
        if user is None or message is None or not message.text:
            return

        if not await self._is_known(str(user.id)):
            # a code-shaped message is always a redemption attempt first
            candidate = message.text.strip().upper()
            if _LINK_CODE_RE.fullmatch(candidate):
                await message.reply_text(await self._redeem(candidate, user))
                return
            if not Config.TELEGRAM_OPEN_ONBOARDING:
                # gated (default): a stranger NEVER reaches chat_service -
                # no user row, no onboarding, no model call
                await message.reply_text(STRANGER_REPLY)
                return
            # open onboarding: fall through into chat_service, which creates
            # the user and starts the introduction - the same trust-on-first-
            # message contract as a discord dm.

        await self._mirror_user_line(str(user.id), message.text)

        from src.models.unified_message import UnifiedMessage

        unified_msg = UnifiedMessage(
            content=message.text,
            platform_user_id=str(user.id),
            platform="telegram",
            platform_message_id=str(message.message_id),
            # the tether always maps to the current daily room - the same
            # shared scope the app posts into, so the routing spine picks
            # the speaker. in private chats chat_id == user_id, so the
            # group target is deliverable as-is.
            chat_scope="group",
            group_chat_id=str(user.id),
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

    async def _mirror_user_line(self, platform_user_id: str, text: str) -> None:
        """one conversation, two windows: the sender's own telegram line,
        fanned best-effort to their connected desktop surfaces before the
        council answers (so the desk sees the question, then the reply).
        strictly cosmetic - the event log is the record; any failure here
        costs the live echo, never the turn."""
        if self.mirror is None:
            return
        try:
            user_uuid = await self.user_manager.lookup_user_uuid(
                "telegram", platform_user_id)
            if user_uuid is None:
                return  # open-onboarding first contact: no user row yet
            await self.mirror.send_message(
                user_uuid, text, speaker="user",
                author_type="user", source_platform="telegram")
        except Exception:
            logger.exception("telegram->app mirror of a user line failed; "
                             "continuing without")

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
        """send a council line to a telegram user, attributed inline and
        chunked. the router forwards `speaker=`; `stream_id` (the room) is
        accepted and ignored - the tether is one room by construction.

        raises UndeliverableError for permanent failures (blocked the bot,
        never /start-ed, malformed id) so the router deactivates the link;
        transient failures return False and leave the link active."""
        try:
            chat_id = int(platform_user_id)
            text = attributed(kwargs.get("speaker"), content)
            chunks = chunk_message(text, max_length=_TELEGRAM_MAX_LENGTH)

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

    async def send_choice(self, platform_user_id: str, content: str,
                          choices: list, **kwargs) -> bool:
        """one message with inline buttons (the rewind tether's ping),
        attributed like any council line. the ping is short by construction -
        no chunking; the buttons must ride the same message they answer.
        failure mapping mirrors send_message."""
        try:
            chat_id = int(platform_user_id)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(c["label"], callback_data=c["data"])]
                for c in choices
            ])
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=attributed(kwargs.get("speaker"), content),
                reply_markup=keyboard)
            return True
        except Forbidden as e:
            raise UndeliverableError(
                f"telegram user {platform_user_id} blocked the bot"
            ) from e
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                raise UndeliverableError(
                    f"telegram chat {platform_user_id} not found (never started?)"
                ) from e
            logger.error(f"telegram bad request sending choice to {platform_user_id}: {e}")
            return False
        except ValueError as e:
            raise UndeliverableError(
                f"invalid telegram user id '{platform_user_id}'"
            ) from e
        except TelegramError as e:
            logger.error(f"transient telegram error sending choice to {platform_user_id}: {e}")
            return False

    async def _on_rewind_choice(
        self, update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """a tap on a tether ping's button. the decision lands server-side
        (first answer wins, tenant-checked); the buttons are then replaced
        with the ack so the question can't be tapped twice by accident.
        the sidecar remains authoritative - this only records the answer."""
        query = update.callback_query
        if query is None or not query.data or update.effective_chat is None:
            return
        ack = await asyncio.to_thread(
            rewind_tether.record_decision_token,
            "telegram", str(update.effective_chat.id), query.data)
        try:
            await query.answer()
        except TelegramError:
            pass
        try:
            base_text = query.message.text if query.message \
                and query.message.text else ""
            await query.edit_message_text(f"{base_text}\n\n{ack}".strip())
        except TelegramError as e:
            logger.warning(f"could not edit tether ping after tap: {e}")

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
