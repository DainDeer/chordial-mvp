"""prompt construction, laid out for prompt caching.

anthropic caching is a byte-exact prefix match, so the prompt is built in
stability zones (most stable first):

  1. tools           - identical every request (rendered before system)
  2. system block 1  - frozen persona; zero interpolation
  3. system block 2  - user profile + core memories; changes rarely
  4. messages        - event history with ABSOLUTE timestamps on USER turns
                       (stable bytes; assistant turns carry no timestamp or
                       markup, so the model doesn't learn to echo any of it
                       into replies), then the current turn carrying all the
                       volatile "now" context

cache breakpoints go on system block 2 (caches tools + system) and on the last
message before the current turn (caches conversation history). the current turn
- the only volatile part - sits after every breakpoint, so it invalidates
nothing before it.

tool ACTIONS from past turns render as bracketed blocks folded into the next
USER-side turn (never onto assistant turns - that pattern taught the model to
echo prefixes into real replies once already). each action line is the frozen
`content` string from its event, emitted verbatim: bytes are fixed at write
time, so replayed history stays cache-stable forever.
"""
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timedelta
import logging
import os

from src.managers.memories_manager import MemoriesManager
from src.managers.event_log import Event
from src.providers.ai.types import AIRequest, ChatTurn, SystemBlock, ToolDef
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

        # standing guidance for the ambient agenda note (the note itself rides in
        # the volatile current turn; this byte-stable line just tells the model
        # how to treat it, so we don't pay the instructions on every turn).
        if Config.agenda_enabled():
            profile_parts.append(
                "- you have quiet, ambient awareness of their notion workspace "
                "(tasks, projects, cycles). a \"notion agenda\" note may ride "
                "along with their messages - treat it as things you happen to "
                "know, not a checklist to recite. bring something up only when "
                "it's relevant or genuinely helpful, one gentle nudge at most, "
                "and use your notion tools when they want details or changes."
            )

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

    def _action_block(self, actions: List[Event], user_timezone: str) -> str:
        """render a run of action events as one bracketed block. attribution is
        third-person (the acting agent's name), so the same format serves a
        multi-persona channel later. lines are the events' frozen content,
        verbatim - never re-serialized."""
        first = actions[0]
        local_ts = to_user_timezone(first.created_at, user_timezone)
        lines = "\n".join(a.content for a in actions)
        return f"[{first.author}'s tool actions - {self._format_ts(local_ts)}:\n{lines}]"

    def _render_history(
        self,
        history: List[Event],
        user_timezone: str,
    ) -> Tuple[List[ChatTurn], List[Event]]:
        """render prior events into chat turns, marking the last one as the
        conversation-history cache breakpoint. returns (turns, leftover_actions)
        - trailing action events with no following user message belong to the
        caller, which folds them into the volatile current turn.

        only USER turns get a timestamp prefix, and action blocks fold into the
        NEXT user turn. assistant turns are rendered verbatim - prefixing them
        taught the model (via its own transcript) that replies start with
        "[day mon dd h:mmam]", which it then occasionally echoed into a real
        reply. bracketed meta on user-side turns is context, not style.
        """
        turns: List[ChatTurn] = []
        pending_actions: List[Event] = []
        for event in history:
            if event.kind == "action":
                pending_actions.append(event)
                continue
            if event.kind != "message":
                continue  # 'note' is reserved, unrendered for now
            if event.role == "user":
                local_ts = to_user_timezone(event.created_at, user_timezone)
                content = f"[{self._format_ts(local_ts)}] {event.content}"
                if pending_actions:
                    content = f"{self._action_block(pending_actions, user_timezone)}\n{content}"
                    pending_actions = []
                turns.append(ChatTurn(role="user", content=content))
            else:
                turns.append(ChatTurn(role="assistant", content=event.content))

        if turns:
            turns[-1].cache = True  # cache the history prefix
        return turns, pending_actions

    @staticmethod
    def _last_user_timestamp(events: List[Event]) -> Optional[datetime]:
        """timestamp (naive utc) of the most recent user message, or None."""
        for event in reversed(events):
            if event.kind == "message" and event.role == "user":
                return event.created_at
        return None

    @staticmethod
    def _format_elapsed(delta: timedelta) -> str:
        """coarse, human-friendly duration: 'less than a minute', '5 minutes',
        '2 hours', '3 days'."""
        secs = max(0, int(delta.total_seconds()))
        if secs < 60:
            return "less than a minute"
        mins = secs // 60
        if mins < 60:
            return f"{mins} minute{'s' if mins != 1 else ''}"
        hours = secs // 3600
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        days = secs // 86400
        return f"{days} day{'s' if days != 1 else ''}"

    def _now_line(
        self,
        user_timezone: str,
        last_user_ts: Optional[datetime],
        user_name: Optional[str],
    ) -> str:
        """the volatile 'now' context: absolute local time, plus how long it's
        been since the user last reached out (so the model can gauge whether
        this is a fresh return or a continuation). deliberately no day-type /
        'vibe' description - the model reads that off the date, and that filler
        was leaking into replies."""
        now_utc = utc_now()
        local_now = to_user_timezone(now_utc, user_timezone)
        line = f"it's {local_now.strftime('%I:%M %p')} on {local_now.strftime('%A, %B %d, %Y')}."
        if last_user_ts is not None:
            who = user_name or "they"
            elapsed = self._format_elapsed(now_utc - last_user_ts)
            line += f" it's been {elapsed} since {who} last messaged you."
        return line

    # --- public builders ---------------------------------------------------

    async def build_conversation_request(
        self,
        conversation_history: List[Event],
        user_name: Optional[str],
        user_uuid: Optional[str],
        user_timezone: str,
        tools: Optional[List[ToolDef]] = None,
        ambient_context: Optional[str] = None,
    ) -> AIRequest:
        """build the request for a reply. the current user message is the LAST
        event in conversation_history; it becomes the volatile 'now' turn.

        `ambient_context` (e.g. the notion agenda digest) rides in that same
        volatile turn, after every cache breakpoint - it changes through the day
        but never touches the cached history/system prefix, because history is
        replayed from stored event content, not from this rendered turn. any
        trailing action events (tools run since the last user message) fold in
        here too, then migrate into the stable history prefix next turn."""
        system = await self._build_system_blocks(user_name, user_uuid, user_timezone)

        prior = conversation_history[:-1] if conversation_history else []
        current = conversation_history[-1] if conversation_history else None

        messages, leftover_actions = self._render_history(prior, user_timezone)

        if current is not None:
            now_line = self._now_line(user_timezone, self._last_user_timestamp(prior), user_name)
            content = f"[current time - {now_line}]\n"
            if leftover_actions:
                content += f"{self._action_block(leftover_actions, user_timezone)}\n"
            if ambient_context:
                content += f"[{ambient_context}]\n"
            content += current.content
            messages.append(ChatTurn(role="user", content=content))

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
        conversation_history: List[Event],
        user_name: Optional[str],
        user_uuid: Optional[str],
        user_timezone: str,
        tools: Optional[List[ToolDef]] = None,
        ambient_context: Optional[str] = None,
    ) -> AIRequest:
        """build the request for a proactive check-in. all history is stable;
        a synthetic 'now' turn carries the generation instructions (plus any
        trailing action events and the ambient agenda context)."""
        system = await self._build_system_blocks(user_name, user_uuid, user_timezone)

        messages, leftover_actions = self._render_history(conversation_history, user_timezone)

        now_line = self._now_line(
            user_timezone, self._last_user_timestamp(conversation_history), user_name
        )
        who = user_name or "them"
        actions_block = (
            f"{self._action_block(leftover_actions, user_timezone)}\n" if leftover_actions else ""
        )
        ambient_block = f"[{ambient_context}]\n" if ambient_context else ""
        messages.append(ChatTurn(
            role="user",
            content=(
                f"[current time - {now_line}]\n"
                f"{actions_block}"
                f"{ambient_block}"
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
