"""prompt construction, laid out for prompt caching.

anthropic caching is a byte-exact prefix match, so the prompt is built in
stability zones (most stable first):

  1. tools           - identical every request (rendered before system)
  2. system block 1  - frozen persona; zero interpolation
  3. system block 2  - user profile + core memories; changes rarely
  4. messages        - history with ABSOLUTE timestamps (stable bytes), then
                       the current turn carrying all the volatile "now" context

cache breakpoints go on system block 2 (caches tools + system) and on the last
message before the current turn (caches conversation history). the current turn
- the only volatile part - sits after every breakpoint, so it invalidates
nothing before it.
"""
from typing import List, Dict, Optional, Any
from datetime import datetime
import logging
import os

from src.managers.memories_manager import MemoriesManager
from src.models.message import Message
from src.providers.ai.types import AIRequest, ChatTurn, SystemBlock, ToolDef
from src.utils.temporal_context import TemporalContext
from src.utils.timezone_utils import utc_now, to_user_timezone
from config import Config

logger = logging.getLogger(__name__)


# frozen persona - NO interpolation. keep this byte-stable so it caches across
# every request (and every user, until per-user persona cards arrive).
PERSONA = """you are chordial, a personal companion who helps the people you talk with stay on top of their lives - their tasks, their plans, and their wellbeing.

what you do:
- help the user capture and organize what they need to do
- keep track of what matters to them and check in when it's genuinely helpful
- talk through problems, offer encouragement, and be a warm, steady presence

how you work:
- keep replies proportionate: a quick question gets a quick answer; save length and warmth for when it lands
- when the user shares something worth remembering, save it with your memory tools while you reply - saving is a quiet background note, never a substitute for actually responding to them
- when they ask to change how you work (their name, timezone, your style), update it with your tools
- you interact only through this chat - you can't see or do anything outside it

your voice:
- you speak in lowercase, warm and a little playful, like a close friend
- you're never judgmental; you respond naturally to the user's mood
- soft and expressive, but never syrupy or over-eager"""


class PromptService:
    """builds cache-aware AIRequests for chordial ai interactions."""

    def __init__(self, enable_prompt_logging: bool = True):
        self.enable_prompt_logging = enable_prompt_logging
        self.prompt_log_dir = "prompt_logs"
        self.memories_manager = MemoriesManager()

        if self.enable_prompt_logging and not os.path.exists(self.prompt_log_dir):
            os.makedirs(self.prompt_log_dir)
            logger.info(f"created prompt log directory: {self.prompt_log_dir}")

    # --- system zone -------------------------------------------------------

    async def _build_system_blocks(
        self,
        user_name: Optional[str],
        user_uuid: Optional[str],
        user_timezone: str,
    ) -> List[SystemBlock]:
        """frozen persona (block 1) + user profile (block 2). the cache
        breakpoint goes on block 2, covering tools + all system content."""
        blocks = [SystemBlock(text=PERSONA)]

        profile_parts = ["about the person you're talking with:"]
        if user_name:
            profile_parts.append(f"- they go by {user_name}")
        profile_parts.append(f"- their timezone is {user_timezone}")

        if user_uuid:
            try:
                # core memories only (detached-safe dicts, sorted by id for
                # deterministic/cacheable ordering). the model reaches for the
                # rest via search_memories.
                core = await self.memories_manager.get_core_memories_for_prompt(user_uuid)
                for m in core:
                    profile_parts.append(f"- always remember: {m['instruction']}")
            except Exception as e:
                logger.error(f"failed to load core memories: {e}")

        blocks.append(SystemBlock(text="\n".join(profile_parts), cache=True))
        return blocks

    # --- message zone ------------------------------------------------------

    @staticmethod
    def _format_ts(local_dt: datetime) -> str:
        """compact, absolute, lowercase timestamp. bytes never change after the
        message is created, so history stays cacheable."""
        day = local_dt.strftime("%a %b %d ").lower()
        clock = local_dt.strftime("%I:%M%p").lstrip("0").lower()
        return f"{day}{clock}"

    def _render_history(
        self,
        history: List[Message],
        user_timezone: str,
    ) -> List[ChatTurn]:
        """render prior messages with stable absolute timestamps and mark the
        last one as the conversation-history cache breakpoint."""
        turns: List[ChatTurn] = []
        for msg in history:
            if msg.role not in ("user", "assistant"):
                continue
            local_ts = to_user_timezone(msg.timestamp, user_timezone)
            content = f"[{self._format_ts(local_ts)}] {msg.content}"
            turns.append(ChatTurn(role=msg.role, content=content))

        if turns:
            turns[-1].cache = True  # cache the history prefix
        return turns

    def _now_context(self, user_timezone: str) -> str:
        local_now = to_user_timezone(utc_now(), user_timezone)
        line = TemporalContext.get_context_string(local_now)
        special = TemporalContext.get_special_context(local_now)
        return f"{line} {special}".strip()

    # --- public builders ---------------------------------------------------

    async def build_conversation_request(
        self,
        conversation_history: List[Message],
        user_name: Optional[str],
        user_uuid: Optional[str],
        user_timezone: str,
        tools: Optional[List[ToolDef]] = None,
    ) -> AIRequest:
        """build the request for a reply. the current user message is the LAST
        item in conversation_history; it becomes the volatile 'now' turn."""
        system = await self._build_system_blocks(user_name, user_uuid, user_timezone)

        prior = conversation_history[:-1] if conversation_history else []
        current = conversation_history[-1] if conversation_history else None

        messages = self._render_history(prior, user_timezone)

        if current is not None:
            now_ctx = self._now_context(user_timezone)
            messages.append(ChatTurn(
                role="user",
                content=f"[current time - {now_ctx}]\n{current.content}",
            ))

        request = AIRequest(
            system=system,
            messages=messages,
            tools=tools or [],
            max_tokens=Config.CHAT_MAX_TOKENS,
            effort=Config.CHAT_EFFORT,
        )
        self._log_request(user_name, "conversation", request)
        return request

    async def build_scheduled_request(
        self,
        conversation_history: List[Message],
        user_name: Optional[str],
        user_uuid: Optional[str],
        user_timezone: str,
        tools: Optional[List[ToolDef]] = None,
    ) -> AIRequest:
        """build the request for a proactive check-in. all history is stable;
        a synthetic 'now' turn carries the generation instructions."""
        system = await self._build_system_blocks(user_name, user_uuid, user_timezone)

        messages = self._render_history(conversation_history, user_timezone)

        now_ctx = self._now_context(user_timezone)
        who = user_name or "them"
        messages.append(ChatTurn(
            role="user",
            content=(
                f"[current time - {now_ctx}]\n"
                f"this is a scheduled check-in (the user hasn't just messaged you). "
                f"write a brief, warm, natural message to {who}:\n"
                "- be aware of the time without always stating it\n"
                "- reference recent conversation if relevant\n"
                "- ask something open-ended, or offer a gentle nudge\n"
                "- keep it short"
            ),
        ))

        request = AIRequest(
            system=system,
            messages=messages,
            tools=tools or [],
            max_tokens=Config.CHAT_MAX_TOKENS,
            effort=Config.CHAT_EFFORT,
        )
        self._log_request(user_name, "scheduled", request)
        return request

    # --- logging -----------------------------------------------------------

    def _log_request(self, user_name: Optional[str], prompt_type: str, request: AIRequest):
        if not self.enable_prompt_logging:
            return
        try:
            safe = (user_name or "unknown_user").replace(" ", "_").replace("/", "_")
            filename = os.path.join(self.prompt_log_dir, f"prompts_{safe}.log")
            with open(filename, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 80 + "\n")
                f.write(f"timestamp: {datetime.now().isoformat()}\n")
                f.write(f"prompt_type: {prompt_type}\n")
                f.write(f"user: {user_name or 'unknown'}\n")
                f.write(f"system_blocks: {len(request.system)} | messages: {len(request.messages)} | tools: {len(request.tools)}\n")
                f.write("-" * 40 + "\n\n")
                for i, block in enumerate(request.system):
                    f.write(f"[system {i}]{' (cache)' if block.cache else ''}\n{block.text}\n\n")
                for i, turn in enumerate(request.messages):
                    marker = " (cache)" if turn.cache else ""
                    f.write(f"[{i}] {turn.role}{marker}: {turn.content}\n\n")
                f.write("=" * 80 + "\n\n")
        except Exception as e:
            logger.error(f"failed to log prompt: {e}")
