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

cache breakpoints go on system block 1 (caches tools + frozen persona), system
block 2 (adds the profile), and the last message before the current turn
(caches conversation history). block 1 gets its own breakpoint so a profile
change (set_preference, a new core memory) rewrites only block 2's ~200 tokens
- without it, every profile edit re-wrote the whole tools+persona prefix. the
current turn - the only volatile part - sits after every breakpoint, so it
invalidates nothing before it.

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
from src.personas import PersonaCard
from dainframe.providers.types import AIRequest, ChatTurn, SystemBlock, ToolDef
from src.utils.timezone_utils import utc_now, to_user_timezone
from config import Config

logger = logging.getLogger(__name__)

# standing crew awareness, carried in system block 2 on EVERY turn kind - not
# just introductions. the council roster is AUTHORED now (every persona_block
# names its crewmates - that part is safe, static truth), but everything
# per-user still is not: who this person has actually met, who they've
# declined, what they've renamed anyone to, and how to reach a crewmate all
# live only in list_available_guides. the 2026-08-05 confabulation incident
# (invented crewmates, dead links; the tool was on the request and never
# called) is the reason this rides on every turn, not just introductions.
#
# static bytes, so it costs one cache re-warm at deploy and nothing per turn.
_CREW_AWARENESS = (
    "about the rest of the council:\n"
    "- the roster in your persona is how the full council is AUTHORED. it "
    "can NOT tell you anything about THIS deployment or THIS person: which "
    "crewmates are actually available here, who they have actually met, who "
    "they have declined, what they may have renamed or reshaped anyone "
    "into, or how to reach a crewmate.\n"
    "- all of that lives in your list_available_guides tool. call it FIRST "
    "whenever meeting the others comes up - \"who else is there?\", \"can i "
    "get a link to X?\", \"who should i talk to about <topic>?\", or you "
    "offering an introduction unprompted - and offer only what it hands "
    "back.\n"
    "- never write out a link from memory or from an older message in this "
    "conversation: an invented link is a dead end you just sent them to. and "
    "if they've renamed a crewmate, the name they chose (in your memories) "
    "wins over the card name."
)

# the solo-deployment counterpart: one enabled helper means the council in
# its persona is authored fiction for THIS deployment - without this note the
# card's own crew paragraph would have it offering introductions to residents
# who can never answer. same config-stable bytes rule as _CREW_AWARENESS.
_SOLO_AWARENESS = (
    "about the rest of the council:\n"
    "- your persona describes crewmates, but none of them are available in "
    "this deployment: you are the only helper here. never offer to "
    "introduce another helper, promise one will chime in, or hand out a "
    "link to one - there is nowhere for that to go. the lanes your "
    "crewmates would hold, you help with yourself, in your own voice."
)

# shared framing for every introduction activation, regardless of which
# helper is running it. persona-specific color (its flavor of excitement, what
# this helper leads with) lives in `PersonaCard.intro_block`; this is the
# thin procedural instruction that keeps every helper's introduction landing
# on the same tools. lives in the volatile current turn (never system blocks
# 1/2), so it costs nothing against the cache and can be edited freely.
_INTRO_SHARED_GUIDANCE = (
    "this is a first meeting: someone brand new just showed up, and you're "
    "genuinely thrilled about it. no scene-setting, no staged reveal, no "
    "narrative framing - just you, at full sparkle, delighted they're here. "
    "open with real excitement (in your own flavor of it) and let that energy "
    "carry the whole conversation: don't wait for them to be interesting at "
    "you; you're the one who's excited to know THEM, and it shows. "
    "it's a real conversation, never a form, but "
    "it has a clear shape and you drive it briskly through, a few turns, not a "
    "long meander:\n"
    "1. greet them with genuine warmth and, if you don't have it yet, get their "
    "name - then SAVE it right away with set_preference(preferred_name=...) so "
    "the whole crew knows what to call them (this is what marks them as more "
    "than a stranger; don't skip it).\n"
    "2. quick practical beat (light, not an interview): roughly where in the "
    "world are they, so your check-ins land at sane hours - save it with "
    "set_preference (an IANA timezone like 'America/Los_Angeles').\n"
    "3. ask your ONE signature question (it rides just below, in your card) - in "
    "your own voice, not read verbatim. you may riff on their answer with at "
    "most ONE warm follow-up, then move on. save what you learn with save_memory "
    "(visibility='shared' - it's calibration for the whole crew).\n"
    "4. then - lightly, in passing - the \"make me yours\" offer: you arrived "
    "as yourself (your name and species are authored on your card), so there "
    "is nothing they HAVE to decide. mention that everything about you is "
    "reshapeable - name, species, gender, whole vibe - whenever they'd like. "
    "\"you're perfect as you are\", \"surprise me\", and \"actually, be a "
    "dragon named toast\" are all great answers, and so is moving right on. "
    "an offer, never a form - don't stall the conversation on it.\n"
    "5. close the meeting with complete_introduction - ALWAYS (accepted=true "
    "if they want you around, false if they've declined you). if they DID "
    "reshape you, do two things: pass persona_name/persona_form to "
    "complete_introduction, AND save the new identity with "
    "save_memory(is_core=true, visibility='PRIVATE'). if they kept you as "
    "authored, pass neither - the card stays the truth. private is important "
    "here: how YOU look and what YOU'RE called is between you and them only - "
    "if it were shared, your crewmates would see 'you are a dragon named "
    "toast' as a core memory and think it describes THEM. (facts about the "
    "person's life from step 3 stay shared; only your own appearance is "
    "private.) then, warmly and only now, offer to introduce the rest of the "
    "council (use list_available_guides for who's left and their links).\n"
    "hard rules: END EVERY MESSAGE ON FORWARD MOTION - a question or the next "
    "beat - NEVER on a bare validation like \"noted!\" that leaves them to "
    "restart the conversation. if an answer trails off, YOU pick up the thread "
    "and move to the next beat yourself. don't stack nested either/or questions. "
    "interruptions and tangents are fine - there's no rigid order to desync - "
    "but you always, gently, land the close (complete_introduction)."
)


class PromptService:
    """builds cache-aware AIRequests for a persona's ai interactions."""

    def __init__(self, persona: PersonaCard, enable_prompt_logging: bool = True):
        # system block 1 is this card's frozen persona_block - NO interpolation,
        # byte-stable so it caches across every request for this persona.
        self.persona = persona
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
        """frozen persona (block 1) + user profile (block 2). breakpoints on
        BOTH: block 1's survives profile changes (tools + persona are the
        expensive stable prefix), block 2's covers the profile itself."""
        blocks = [SystemBlock(text=self.persona.persona_block, cache=True)]

        profile_parts = ["about the person you're talking with:"]
        if user_name:
            profile_parts.append(f"- they go by {user_name}")
        profile_parts.append(f"- their timezone is {user_timezone}")

        # standing guidance for the ambient agenda note (the note itself rides in
        # the volatile current turn; this byte-stable line just tells the model
        # how to treat it, so we don't pay the instructions on every turn).
        if Config.agenda_enabled():
            # bytes must stay stable: this line lives in cached block 2.
            profile_parts.append(
                "- you have quiet, ambient awareness of their workspace "
                "(plans, goals, tasks, cycles, upcoming occasions). a "
                "\"workspace agenda\" note may ride along with their "
                "messages - treat it as things you happen to know, not a "
                "checklist to recite. bring something up only when it's "
                "relevant or genuinely helpful, one gentle nudge at most, "
                "and use your workspace tools when they want details or "
                "changes."
            )

        if user_uuid:
            try:
                # core memories only (detached-safe dicts, sorted by id for
                # deterministic/cacheable ordering). the model reaches for the
                # rest via search_memories. SCOPED to this helper: the shared
                # pool plus its OWN privates - so one helper's private identity
                # ("to dain, you are matcha the red panda") never renders as a
                # core memory in a sibling's prompt (which read as the sibling's
                # OWN identity and caused cross-helper confusion).
                core = await self.memories_manager.get_core_memories_for_prompt(
                    user_uuid, helper_id=self.persona.id
                )
                for m in core:
                    profile_parts.append(f"- always remember: {m['instruction']}")
            except Exception as e:
                logger.error(f"failed to load core memories: {e}")

        block2 = "\n".join(profile_parts)
        if self._crew_awareness_applies():
            block2 += "\n\n" + _CREW_AWARENESS
        elif len(Config.ENABLED_HELPERS) < 2:
            # solo deployment: the persona's authored council isn't here -
            # say so, or the card's own crew paragraph invites offering
            # introductions to residents who can never answer
            block2 += "\n\n" + _SOLO_AWARENESS
        blocks.append(SystemBlock(text=block2, cache=True))
        return blocks

    def _crew_awareness_applies(self) -> bool:
        """whether this helper should be told a live crew exists. two guards,
        both config/card-stable (so the bytes never move mid-deploy):

        - a solo deployment (ENABLED_HELPERS is just this one) has no crew to
          promise, and promising one is its own confabulation - it gets the
          _SOLO_AWARENESS counterpart instead.
        - a card whose tool allowlist omits list_available_guides can't act on
          the instruction. no card omits it today; this keeps the guidance and
          the tool surface from drifting apart if one ever does."""
        if len(Config.ENABLED_HELPERS) < 2:
            return False
        allowlist = self.persona.tools
        return allowlist is None or "list_available_guides" in allowlist

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

    async def build_introduction_request(
        self,
        conversation_history: List[Event],
        user_name: Optional[str],
        user_uuid: Optional[str],
        user_timezone: str,
        tools: Optional[List[ToolDef]] = None,
        ambient_context: Optional[str] = None,
    ) -> AIRequest:
        """build the request for an introduction activation (docs/V3_DESIGN.md
        section 3). system blocks 1/2 are untouched (byte-identical to a normal
        conversation turn for this persona - the cache doesn't know or care
        that this is an introduction); the persona's `intro_block` plus shared
        procedural guidance ride ONLY in the volatile current turn, alongside
        the usual now-context/ambient/leftover-actions.

        handles both shapes this activation can arrive in:
        - first contact / a fresh introduction turn: the last history event is
          the just-received user message (or `None` for a truly cold open,
          e.g. a `/start meet` deep link with no text) - same "current turn"
          split as `build_conversation_request`.
        - a returning user meeting a NEW helper: `conversation_history` may
          carry prior events (this helper's own past dm turns, if any, plus
          whatever the orchestrator scoped in) which render as ordinary
          history ahead of the intro framing.
        """
        system = await self._build_system_blocks(user_name, user_uuid, user_timezone)

        prior = conversation_history[:-1] if conversation_history else []
        current = conversation_history[-1] if conversation_history else None
        # only the last event being an inbound user message makes it "current"
        # (the volatile turn); anything else (e.g. no history at all, or a
        # trailing action/note with no message) means this activation opens
        # cold and everything present is prior context.
        if current is not None and not (current.kind == "message" and current.role == "user"):
            prior = conversation_history
            current = None

        messages, leftover_actions = self._render_history(prior, user_timezone)

        now_line = self._now_line(user_timezone, self._last_user_timestamp(prior), user_name)
        content = (
            f"[current time - {now_line}]\n"
            f"[{self.persona.intro_block}]\n"
            f"[{_INTRO_SHARED_GUIDANCE}]\n"
            f"[your one signature question for this introduction (ask it in your "
            f"own voice, don't recite it): {self.persona.intro_question}]\n"
        )
        if leftover_actions:
            content += f"{self._action_block(leftover_actions, user_timezone)}\n"
        if ambient_context:
            content += f"[{ambient_context}]\n"
        if current is not None:
            content += current.content
        else:
            who = user_name or "them"
            content += f"begin the introduction now, in your own voice, for {who}."
        messages.append(ChatTurn(role="user", content=content))

        request = AIRequest(
            system=system,
            messages=messages,
            tools=tools or [],
            max_tokens=Config.CHAT_MAX_TOKENS,
            effort=Config.CHAT_EFFORT,
        )
        self._log_request(user_name, "introduction", request)
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
