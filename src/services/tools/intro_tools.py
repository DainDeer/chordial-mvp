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
from src.providers.ai.types import ToolDef
from .base import Tool
from .context import current_helper

logger = logging.getLogger(__name__)

_helper_states = HelperStateManager()


async def _complete_introduction(tool_input: dict, user_uuid: str) -> str:
    accepted = bool(tool_input.get("accepted", False))
    persona_name = tool_input.get("persona_name")
    persona_form = tool_input.get("persona_form")
    # normalize blank strings to None - "declined" reads the same whether the
    # model omitted the field or passed an empty string
    persona_name = persona_name.strip() if isinstance(persona_name, str) and persona_name.strip() else None
    persona_form = persona_form.strip() if isinstance(persona_form, str) and persona_form.strip() else None

    helper_id = current_helper()
    await _helper_states.complete_introduction(
        user_uuid, helper_id,
        accepted=accepted,
        persona_name=persona_name,
        persona_form=persona_form,
    )

    if not accepted:
        return f"recorded: {helper_id} was declined for this user - no longer part of their active crew."
    if persona_name:
        return f"recorded: {helper_id} is now active for this user, known to them as {persona_name}."
    return f"recorded: {helper_id} is now active for this user (no character/name chosen)."


COMPLETE_INTRODUCTION = Tool(
    definition=ToolDef(
        name="complete_introduction",
        description=(
            "Call this ONCE the person has settled on how you should appear to "
            "them (a name/form, no preference, or 'no character at all') - or "
            "once they've made clear they don't want this helper around. This "
            "stamps the relationship state the rest of the crew (and the "
            "director) reads. It does NOT save the identity itself: separately "
            "call save_memory(is_core=true, visibility='private') with something "
            "like \"to <user>, you are <name>, a <form>\" (or a note that they "
            "chose no character) - do both. Use visibility='private': your own "
            "look/name is between you and this person, so your crewmates don't "
            "see 'you are a red panda' and mistake it for their OWN identity."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "accepted": {
                    "type": "boolean",
                    "description": "False if the person doesn't want this helper in their crew at all. True otherwise (including 'no character, please' - they still want the help, just not a persona).",
                },
                "persona_name": {
                    "type": ["string", "null"],
                    "description": "The chosen name, if any. Null if they declined a character/name, or if accepted is false.",
                },
                "persona_form": {
                    "type": ["string", "null"],
                    "description": "The chosen form/species/vibe, if any (e.g. 'red panda', 'no character'). Null if not applicable.",
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


async def _list_available_guides(tool_input: dict, user_uuid: str) -> str:
    acting = current_helper()
    cards = load_personas()

    lines = []
    for helper_id, card in sorted(cards.items()):
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
        bits = f"- {helper_id} ({card.archetype}) - {card.specialty}"
        if username:
            bits += f" - meet them: {_deep_link(username)}"
        lines.append(bits)

    if not lines:
        return "no other guides left to introduce - everyone's already been met (or passed on)."
    return "guides not yet met by this user:\n" + "\n".join(lines)


LIST_AVAILABLE_GUIDES = Tool(
    definition=ToolDef(
        name="list_available_guides",
        description=(
            "Look up which other helpers in the crew this user hasn't met yet "
            "(or has met but hasn't decided about) - each with its specialty "
            "and a deep link that opens that helper's own chat and starts its "
            "introduction. Use this when offering to introduce the other "
            "guides, e.g. after your own introduction wraps up."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_list_available_guides,
    record_event=False,  # pure read: the roster goes stale immediately
)
