"""introduction tools: the thin state spine under the storytelling onboarding
overhaul (docs/V3_DESIGN.md section 3).

the prose, pacing, and interpretation of "getting to know someone" are the
model's job; these two tools are the only code-side bookkeeping it needs:
`complete_introduction` stamps the (user, helper) relationship's final state
once identity has settled (or the user declined), and `list_available_guides`
lets the acting helper see who else in the crew hasn't been met yet, so it can
offer introductions and hand over a working deep link.
"""
import logging

from config import Config
from src.managers.helper_state_manager import HelperStateManager
from src.personas import load_personas
from dainframe.providers.types import ToolDef
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool
from src.services.identity import user_of_context

logger = logging.getLogger(__name__)

_helper_states = HelperStateManager()


async def _complete_introduction(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    accepted = bool(tool_input.get("accepted", False))
    persona_name = tool_input.get("persona_name")
    persona_form = tool_input.get("persona_form")
    # normalize blank strings to None - "declined" reads the same whether the
    # model omitted the field or passed an empty string
    persona_name = persona_name.strip() if isinstance(persona_name, str) and persona_name.strip() else None
    persona_form = persona_form.strip() if isinstance(persona_form, str) and persona_form.strip() else None

    helper_id = context.actor
    await _helper_states.complete_introduction(
        user_uuid, helper_id,
        accepted=accepted,
        persona_name=persona_name,
        persona_form=persona_form,
    )

    if not accepted:
        return f"recorded: {helper_id} was declined for this user - no longer part of their active council."
    if persona_name:
        return f"recorded: {helper_id} is now active for this user, reshaped as {persona_name}."
    return f"recorded: {helper_id} is now active for this user (authored identity kept)."


COMPLETE_INTRODUCTION = Tool(
    definition=ToolDef(
        name="complete_introduction",
        description=(
            "Call this ONCE at the close of an introduction - always. It "
            "stamps the relationship state the rest of the council (and the "
            "director) reads. You arrive with an AUTHORED identity (the name "
            "and species on your card): if the person keeps you as you are, "
            "pass accepted=true and NOTHING else - the card stays the truth. "
            "Only if they RESHAPED you (a new name, species, or vibe) pass "
            "persona_name/persona_form with what they chose, and separately "
            "save it with save_memory(is_core=true, visibility='private') as "
            "something like \"to <user>, you are <name>, a <form>\" - do "
            "both. Use visibility='private': your own look/name is between "
            "you and this person, so your crewmates don't see 'you are a "
            "dragon named toast' and mistake it for their OWN identity."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "accepted": {
                    "type": "boolean",
                    "description": "False if the person doesn't want this helper in their council at all. True otherwise (including keeping the authored identity untouched).",
                },
                "persona_name": {
                    "type": ["string", "null"],
                    "description": "ONLY a name the person chose to replace your authored one. Null if they kept you as authored, or if accepted is false.",
                },
                "persona_form": {
                    "type": ["string", "null"],
                    "description": "ONLY a form/species/vibe the person chose to replace your authored one (e.g. 'red panda'). Null if they kept you as authored, or if accepted is false.",
                },
            },
            "required": ["accepted"],
        },
    ),
    handler=_complete_introduction,
    terminal=True,   # a state-machine side effect - don't discard the reply to run it
    record_event=True,
)


def _deep_link(handle: str) -> str:
    # the 'meet' payload is what the telegram interface routes to
    # begin_introduction (telegram_bot._MEET_PAYLOAD) - keep them in sync.
    return f"https://t.me/{handle}?start=meet"


async def _list_available_guides(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    acting = context.actor
    cards = load_personas()

    lines = []
    offered: list[tuple[str, bool]] = []
    # only helpers that are actually DEPLOYED (ENABLED_HELPERS) are offered -
    # every card is authored for the full council, but a solo or partial
    # deployment must not advertise residents who can never answer (or hand
    # out their deep links from leftover env vars).
    for helper_id in sorted(set(Config.ENABLED_HELPERS) & set(cards)):
        card = cards[helper_id]
        if helper_id == acting:
            continue
        state = await _helper_states.get(user_uuid, helper_id)
        if state.status in ("active", "declined"):
            continue
        # the real, registered @username (config) - NEVER the persona card's
        # telegram_handle placeholder, which is almost never the actual name
        # a helper's bot got registered under (botfather names are globally
        # unique). a helper with no telegram bot configured at all has no
        # deep link to offer - still worth listing, just without one.
        username = Config.telegram_username_for(helper_id)
        bits = (f"- {card.emoji} {helper_id} the {card.species} - "
                f"{card.specialty}")
        if username:
            bits += f" - meet them: {_deep_link(username)}"
        lines.append(bits)
        offered.append((helper_id, bool(username)))

    # the audit trail for this tool. record_event stays False (a roster goes
    # stale immediately and would sit in cache-stable history forever, inviting
    # exactly the stale-reread this tool exists to prevent), so the log line IS
    # the record of what the model was handed - and, by its absence, of the
    # turns where a helper answered a roster question without asking.
    logger.info(
        "list_available_guides: actor=%s user=%s -> %s",
        acting, user_uuid,
        ", ".join(f"{h}{'' if l else ' (no link)'}" for h, l in offered) or "none",
    )

    if not lines:
        return "no other guides left to introduce - everyone's already been met (or passed on)."
    return "guides not yet met by this user:\n" + "\n".join(lines)


LIST_AVAILABLE_GUIDES = Tool(
    definition=ToolDef(
        name="list_available_guides",
        description=(
            "The ONLY source of truth for who else is in this person's crew. "
            "Returns the helpers they haven't met yet (or have met but haven't "
            "decided about), each with its real specialty and a working deep "
            "link that opens that helper's own chat and starts its "
            "introduction.\n"
            "\n"
            "Call this ANY time the other helpers come up - not just at the end "
            "of your own introduction. That includes: \"who else is there?\", "
            "\"introduce me to the other helpers / companions / guides / "
            "crew\", \"can I get a link to X?\", \"who should I talk to about "
            "<topic>?\", or you offering the crew unprompted.\n"
            "\n"
            "You do not know the roster without calling this, and a plausible "
            "guess is worse than asking: crewmate names, what each one "
            "specializes in, and every link are known ONLY here. Never write a "
            "crewmate's name, specialty, or URL from memory or from an earlier "
            "message - an invented helper doesn't exist and an invented link "
            "goes nowhere. Call it fresh each time; who's been met changes."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_list_available_guides,
    # pure read: the roster goes stale immediately, and recording it would park
    # a frozen copy in cache-stable history for the model to answer FROM next
    # time instead of asking again. auditability comes from the handler's log
    # line plus agent_traces.tool_trace (which records every call, tool-by-tool,
    # whatever this flag says).
    record_event=False,
)
