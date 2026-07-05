"""preference tools: let the user reconfigure the bot through conversation.

since chat is the only UI, configuration should *be* conversation - "call me
Dee" or "my timezone is US/Pacific" should just work. this covers the settings
that take effect immediately today; schedule/quiet-hours wiring lands with
scheduler v2.
"""
import logging

import pytz

from src.managers.user_manager import UserManager
from src.providers.ai.types import ToolDef
from .base import Tool

logger = logging.getLogger(__name__)

_users = UserManager()

_VALID_PERSONALITIES = {"friendly", "professional", "cheerful", "calm"}


async def _set_preference(tool_input: dict, user_uuid: str) -> str:
    updates: dict = {}
    notes: list[str] = []

    name = tool_input.get("preferred_name")
    if name:
        updates["preferred_name"] = name.strip()
        notes.append(f"call you {name.strip()}")

    tz = tool_input.get("timezone")
    if tz:
        try:
            pytz.timezone(tz)
        except pytz.UnknownTimeZoneError:
            return (
                f"'{tz}' isn't a timezone i recognize. use an IANA name like "
                "'US/Pacific', 'America/New_York', or 'Europe/London'."
            )
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
            "work. Use for: what to call them (preferred_name), their timezone "
            "(so check-ins and time references land right), and your "
            "conversational style (bot_personality). Only include fields the "
            "user actually asked to change."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "preferred_name": {
                    "type": "string",
                    "description": "What the user wants to be called.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name, e.g. 'US/Pacific', 'Europe/London'.",
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
)
