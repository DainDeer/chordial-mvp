"""memory tools: let the model persist and recall facts about the user.

replaces v1's implicit "inject inferred memories" pipeline with explicit,
model-driven capture. the model deciding "that's worth remembering" mid-chat
is more accurate and more transparent (it can say "noted 💛") than a background
inference job.
"""
import logging

from src.managers.memories_manager import MemoriesManager, MemoryType, MemorySource
from src.providers.ai.types import ToolDef
from .base import Tool
from .context import current_helper

logger = logging.getLogger(__name__)

_memories = MemoriesManager()

_VALID_TYPES = {t.value for t in MemoryType}
_VALID_VISIBILITY = {"shared", "private"}


async def _save_memory(tool_input: dict, user_uuid: str) -> str:
    instruction = (tool_input.get("instruction") or "").strip()
    if not instruction:
        return "nothing to save - `instruction` was empty."

    raw_type = (tool_input.get("memory_type") or "FACT").upper()
    memory_type = MemoryType(raw_type) if raw_type in _VALID_TYPES else MemoryType.FACT

    is_core = bool(tool_input.get("is_core", False))
    keywords = tool_input.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]

    # visibility: 'shared' (default) joins the pool every helper can see;
    # 'private' stays between this helper and the user. attribution is always
    # the acting helper (from the tool-loop context), even for shared rows.
    visibility = (tool_input.get("visibility") or "shared").lower()
    if visibility not in _VALID_VISIBILITY:
        visibility = "shared"

    result = await _memories.upsert_memory(
        user_uuid=user_uuid,
        ai_instruction=instruction,
        memory_type=memory_type,
        source=MemorySource.AI_INFERRED,
        keywords=keywords,
        core=is_core,
        created_by=current_helper(),
        visibility=visibility,
    )

    if result.action == "reinforced":
        # a near-duplicate already existed - we bumped its importance instead of
        # storing a second copy. tell the model so it doesn't try to "fix" it.
        return (
            f"you already had a very similar memory, so i reinforced it "
            f"(now seen {result.times_seen}x, importance {result.weighting:.0f}): "
            f"{result.instruction}"
        )
    return f"saved{' core' if is_core else ''} memory: {instruction}"


async def _search_memories(tool_input: dict, user_uuid: str) -> str:
    terms = tool_input.get("keywords") or []
    if isinstance(terms, str):
        terms = [t.strip() for t in terms.split(",") if t.strip()]
    if not terms:
        return "no search keywords provided."

    # scoped to what this helper may see: the shared pool plus its own privates.
    matches = await _memories.search_memories_by_keywords(
        user_uuid, terms, helper_id=current_helper(),
    )
    if not matches:
        return "no memories matched those keywords."
    return "\n".join(_render_match(m) for m in matches)


def _render_match(m) -> str:
    """a search hit, tagged with its source helper when a SIBLING saved it -
    'heard from aria that ...' is the shared-memory gossip channel made legible.
    a helper's own memories render unattributed (it already knows they're its)."""
    created_by = getattr(m, "created_by", None) or "chordial"
    src = "" if created_by == current_helper() else f" (from {created_by})"
    return f"- [{m.memory_type}]{src} {m.ai_instruction}"


SAVE_MEMORY = Tool(
    definition=ToolDef(
        name="save_memory",
        description=(
            "Save a durable fact, preference, or note about the user so you can "
            "recall it in future conversations. Call this whenever the user "
            "shares something worth remembering (a preference, an important "
            "detail about their life, a recurring goal). Set is_core=true only "
            "for identity-level facts that should always be front-of-mind."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "The thing to remember, phrased as a note to yourself.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["PREFERENCE", "FACT", "EPISODIC"],
                    "description": "PREFERENCE = how they like to be treated; FACT = stable info about them; EPISODIC = a passing event/context.",
                },
                "is_core": {
                    "type": "boolean",
                    "description": "True only for always-remember identity facts.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "A few search keywords for later recall.",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["shared", "private"],
                    "description": "shared (default) = the whole crew can recall it; use it for real facts about the user's life. private = kept just between you and them, for relationship texture the others don't need.",
                },
            },
            "required": ["instruction"],
        },
    ),
    handler=_save_memory,
    terminal=True,  # saving is a side effect - don't discard the reply to do it
)


SEARCH_MEMORIES = Tool(
    definition=ToolDef(
        name="search_memories",
        description=(
            "Look up things you've previously saved about the user by keyword. "
            "Use this when the conversation touches on something you might have "
            "noted before but don't currently see in context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords to search saved memories for.",
                },
            },
            "required": ["keywords"],
        },
    ),
    handler=_search_memories,
    record_event=False,  # pure read: results go stale, nothing to dedup
)
