"""preference tools: let the user reconfigure the bot through conversation.

since chat is the only UI, configuration should *be* conversation - "call me
Dee" or "my timezone is US/Pacific" should just work. this covers the settings
that take effect immediately today; schedule/quiet-hours wiring lands with
scheduler v2.
"""
import logging

import pytz

from src.managers.user_manager import UserManager
from src.utils.timezone_utils import canonicalize_timezone
from dainframe.providers.types import ToolDef
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool
from src.services.identity import user_of_context

logger = logging.getLogger(__name__)

_users = UserManager()

_VALID_PERSONALITIES = {"friendly", "professional", "cheerful", "calm"}


async def _set_preference(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    updates: dict = {}
    notes: list[str] = []

    name = tool_input.get("preferred_name")
    if name:
        updates["preferred_name"] = name.strip()
        notes.append(f"call you {name.strip()}")

    pronouns = tool_input.get("pronouns")
    if pronouns:
        # stored verbatim, deliberately unvalidated - there is no list of
        # acceptable answers here, and rejecting one would be the single most
        # alienating thing this tool could do
        updates["pronouns"] = pronouns.strip()
        notes.append(f"use {pronouns.strip()} for you")

    tz = tool_input.get("timezone")
    if tz:
        try:
            pytz.timezone(tz)
        except pytz.UnknownTimeZoneError:
            return (
                f"'{tz}' isn't a timezone i recognize. use an IANA name like "
                "'America/Los_Angeles', 'America/New_York', or 'Europe/London'."
            )
        # legacy aliases ("US/Pacific") validate under pytz but break
        # stdlib-zoneinfo consumers - store the canonical name instead
        tz = canonicalize_timezone(tz)
        updates["timezone"] = tz
        notes.append(f"set your timezone to {tz}")

    personality = tool_input.get("bot_personality")
    if personality:
        personality = personality.lower().strip()
        if personality not in _VALID_PERSONALITIES:
            return f"i can be one of: {', '.join(sorted(_VALID_PERSONALITIES))}."
        updates["bot_personality"] = personality
        notes.append(f"switch my style to {personality}")

    if not updates:
        return "no recognized preferences to update."

    await _users.update_user_preferences(user_uuid, updates)
    return "updated: " + "; ".join(notes)


SET_PREFERENCE = Tool(
    definition=ToolDef(
        name="set_preference",
        description=(
            "Update the user's settings when they ask you to change how you "
            "work. Use for: what to call them (preferred_name), how to refer "
            "to them (pronouns), their timezone (so check-ins and time "
            "references land right), and your conversational style "
            "(bot_personality). Only include fields the user actually asked to "
            "change. If they tell you a new name or new pronouns at any point, "
            "call this immediately - don't wait to be asked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "preferred_name": {
                    "type": "string",
                    "description": "What the user wants to be called.",
                },
                "pronouns": {
                    "type": "string",
                    "description": (
                        "The user's pronouns, exactly as they said them, e.g. "
                        "'she/her', 'they/them', 'he/they', 'any'. Free text - "
                        "record what they actually said rather than mapping it "
                        "onto an expected set."
                    ),
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name, e.g. 'America/Los_Angeles', 'Europe/London'.",
                },
                "bot_personality": {
                    "type": "string",
                    "enum": ["friendly", "professional", "cheerful", "calm"],
                    "description": "The conversational style the user prefers from you.",
                },
            },
        },
    ),
    handler=_set_preference,
    terminal=True,  # updating a setting is a side effect - keep the reply
)
