"""observation tools: the council's quiet, structured noticing.

the phase-3 emit path for 'helpers reason more than they speak'
(ROOMS_DESIGN.md §2/§3). record_observation is deliberately NOT save_memory:
a memory is a fact about the person that renders into every future prompt;
an observation is the acting helper's own evidence-tied read of how things
are going, filed for reviews and edwin's scorecards. the tool descriptions
carry that boundary hard, because a model that blurs it double-writes.

attribution mirrors the other tools: WHICH helper observed comes from the
tool-loop context (never model input), the user from the identity split's
metadata, and the room from the stream the turn is running in.
"""
import logging

from dainframe.providers.types import ToolDef
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool

from src.services.identity import user_of_context
from src.services.observations import OBSERVATION_KINDS, ObservationStore

logger = logging.getLogger(__name__)

_store = ObservationStore()


async def _record_observation(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    kind = (tool_input.get("kind") or "").strip()
    content = (tool_input.get("content") or "").strip()
    evidence = (tool_input.get("evidence") or "").strip()
    try:
        row = _store.record(
            user_uuid,
            context.actor,
            kind,
            content,
            # the stream this turn runs in IS the room uuid (legacy streams
            # grandfathered to the user uuid - still a room)
            room_uuid=context.stream_id,
            evidence={"note": evidence} if evidence else None,
        )
    except ValueError as e:
        return str(e)
    return f"observed ({row['kind']}): {row['content']}"


RECORD_OBSERVATION = Tool(
    definition=ToolDef(
        name="record_observation",
        description=(
            "Quietly file YOUR OWN noticing about how things are going for "
            "this person - a pattern you've seen form, progress worth "
            "crediting, a concern building, or context that explains a "
            "stretch of days. Observations feed the council's reviews and "
            "scorecards; they are never shown back as conversation.\n"
            "\n"
            "This is NOT save_memory, and the line matters: a MEMORY is a "
            "fact about the person (\"allergic to walnuts\", \"prefers "
            "morning focus blocks\") that you'll need in future chats. An "
            "OBSERVATION is your professional read of what's happening "
            "(\"third week running she plans music practice on days with "
            "evening meetings and it never survives\"). Facts -> "
            "save_memory. Noticing -> record_observation. When you use "
            "this, keep replying normally - filing is silent bookkeeping, "
            "never the response itself."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(OBSERVATION_KINDS),
                    "description": (
                        "pattern = a recurring shape in how they work/live; "
                        "progress = real movement worth crediting at review "
                        "time; concern = something building that the "
                        "council should watch; context = a fact about this "
                        "stretch of time (travel, illness, crunch) that "
                        "explains the numbers."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The noticing itself, one or two sentences, specific "
                        "enough to be useful at a review weeks later."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Optional: what this is tied to - their words, the "
                        "concrete events. Observations are evidence-tied; "
                        "an unanchored vibe belongs nowhere."
                    ),
                },
            },
            "required": ["kind", "content"],
        },
    ),
    handler=_record_observation,
    terminal=True,   # a side effect - don't discard the reply to run it
    record_event=True,
)


async def _list_observations(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    helper_id = tool_input.get("helper")
    if helper_id == "mine":
        helper_id = context.actor
    kind = tool_input.get("kind")
    rows = _store.recent(
        user_uuid,
        helper_id=helper_id,
        kind=kind,
        limit=int(tool_input.get("limit") or 20),
    )
    if not rows:
        return "no observations recorded (for that filter) yet."
    lines = []
    for r in reversed(rows):  # oldest -> newest reads like a story
        when = r["created_at"].date().isoformat() if r["created_at"] else "?"
        line = f"- [{when}] {r['helper_id']} ({r['kind']}): {r['content']}"
        if r["evidence"]:
            note = r["evidence"].get("note") if isinstance(r["evidence"], dict) else None
            if note:
                line += f" (evidence: {note})"
        lines.append(line)
    return f"{len(rows)} observation(s), oldest first:\n" + "\n".join(lines)


LIST_OBSERVATIONS = Tool(
    definition=ToolDef(
        name="list_observations",
        description=(
            "Read the council's filed observations about this person - "
            "patterns, progress, concerns, context - newest window, oldest "
            "first. Use when reviewing how a stretch of time actually went, "
            "or before making a claim about a trend: observations are the "
            "evidence trail. Filter helper='mine' for only your own."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "helper": {
                    "type": "string",
                    "description": "Filter by observer id, or 'mine'.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(OBSERVATION_KINDS),
                },
                "limit": {"type": "integer", "description": "Default 20, max 100."},
            },
        },
    ),
    handler=_list_observations,
    record_event=False,  # pure read: results go stale, nothing to dedup
)
