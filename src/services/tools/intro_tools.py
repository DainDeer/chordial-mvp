"""introduction tools: the thin state spine under the storytelling onboarding
overhaul (docs/V3_DESIGN.md section 3).

the prose, pacing, and interpretation of "getting to know someone" are the
model's job; these tools are the only code-side bookkeeping it needs:
`complete_introduction` stamps the (user, helper) relationship's final state
once the chair's front door has settled identity (or the user declined);
`list_available_guides` lets the acting helper see who else in the crew
hasn't been met yet, so it can offer introductions; and `meet_guide` is the
offer accepted - it moves the chosen guide to active so the director can
give them the floor. (the ensemble-era per-guide telegram deep links and
their private dm rituals retired with the per-helper bots in phase 7a:
guides are met in the rooms, where the council already speaks, and
meet_guide is the transition that makes that real - the director only casts
active helpers, so without it a guide would stay unmeetable forever.)
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


async def _list_available_guides(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    acting = context.actor
    cards = load_personas()

    lines = []
    offered: list[str] = []
    # only helpers that are actually DEPLOYED (ENABLED_HELPERS) are offered -
    # every card is authored for the full council, but a solo or partial
    # deployment must not advertise residents who can never answer.
    for helper_id in sorted(set(Config.ENABLED_HELPERS) & set(cards)):
        card = cards[helper_id]
        if helper_id == acting:
            continue
        state = await _helper_states.get(user_uuid, helper_id)
        if state.status in ("active", "declined"):
            continue
        lines.append(f"- {card.emoji} {helper_id} the {card.species} - "
                     f"{card.specialty}")
        offered.append(helper_id)

    # the audit trail for this tool. record_event stays False (a roster goes
    # stale immediately and would sit in cache-stable history forever, inviting
    # exactly the stale-reread this tool exists to prevent), so the log line IS
    # the record of what the model was handed - and, by its absence, of the
    # turns where a helper answered a roster question without asking.
    logger.info(
        "list_available_guides: actor=%s user=%s -> %s",
        acting, user_uuid, ", ".join(offered) or "none",
    )

    if not lines:
        return "no other guides left to introduce - everyone's already been met (or passed on)."
    return "guides not yet met by this user:\n" + "\n".join(lines)


LIST_AVAILABLE_GUIDES = Tool(
    definition=ToolDef(
        name="list_available_guides",
        description=(
            "The ONLY source of truth for who else is in this person's crew. "
            "Returns the helpers they haven't met yet (or have met but "
            "haven't decided about), each with its real specialty. Meeting "
            "happens right here in the room: when the person says yes to "
            "one, call meet_guide with that guide's id.\n"
            "\n"
            "Call this ANY time the other helpers come up - not just at the end "
            "of your own introduction. That includes: \"who else is there?\", "
            "\"introduce me to the other helpers / companions / guides / "
            "crew\", \"who should I talk to about <topic>?\", or you "
            "offering the crew unprompted.\n"
            "\n"
            "You do not know the roster without calling this, and a plausible "
            "guess is worse than asking: crewmate names and what each one "
            "specializes in are known ONLY here. Never write a crewmate's "
            "name or specialty from memory or from an earlier message - an "
            "invented helper doesn't exist. Call it fresh each time; who's "
            "been met changes."
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


async def _meet_guide(tool_input: dict, context: ToolContext) -> str:
    """the offer accepted: move a chosen guide to active so the director can
    cast them from the next turn on. meeting happens IN the room - there is
    no private ritual to run - so the caller's job after this returns is
    simply to welcome them in. declined and disabled guides are re-meetable
    by design (the states mean "not right now", and this tool only ever runs
    because the user just asked)."""
    user_uuid = user_of_context(context)
    guide_id = tool_input.get("guide_id")
    guide_id = guide_id.strip().lower() if isinstance(guide_id, str) else ""

    cards = load_personas()
    if guide_id == context.actor:
        return "you can't meet yourself - you're already here."
    if guide_id not in set(Config.ENABLED_HELPERS) & set(cards):
        # undeployed and misspelled answer identically: not in this house.
        # list_available_guides is the source of truth for who is.
        return (f"refused: there is no guide '{guide_id or '?'}' in this "
                "house. call list_available_guides for who's actually here.")

    state = await _helper_states.get(user_uuid, guide_id)
    if state.status == "active":
        return f"{guide_id} is already part of this person's council - just talk to them."

    await _helper_states.set_status(user_uuid, guide_id, "active")
    card = cards[guide_id]
    logger.info("meet_guide: actor=%s user=%s guide=%s (was %s)",
                context.actor, user_uuid, guide_id, state.status)
    return (f"{card.emoji} {guide_id} the {card.species} has joined this "
            f"person's council ({card.specialty}). they can speak in this "
            "room from the next turn - welcome them in.")


MEET_GUIDE = Tool(
    definition=ToolDef(
        name="meet_guide",
        description=(
            "Add a guide to this person's active council when they say yes "
            "to meeting one - the moment they agree (\"yes, introduce me to "
            "remy\", \"can pip join?\"), call this with that guide's id. "
            "Meeting happens right here in the room: after this returns, "
            "welcome the guide in your reply, and they can speak from the "
            "next turn. Guide ids come from list_available_guides - call it "
            "first if you're not sure who's available. Also the way back for "
            "a guide the person previously declined or turned off, when they "
            "ask for them again. Never call it unprompted: joining the "
            "council is the person's choice."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "guide_id": {
                    "type": "string",
                    "description": "The guide's id exactly as "
                                   "list_available_guides names it (e.g. 'remy').",
                },
            },
            "required": ["guide_id"],
        },
    ),
    handler=_meet_guide,
    # a durable state change the whole council reads: the frozen action line
    # is how next turn's speaker knows the meeting already happened.
    record_event=True,
)
