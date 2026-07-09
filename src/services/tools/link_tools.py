"""the link_platform tool: chordial mints a one-time code (and telegram deep
link) so the user can connect another platform to this same conversation.

deliberately NOT terminal: the tool result must round-trip so the model SEES
the code and weaves it into its reply. record_event stays True - the frozen
action line lets the model know next turn that it already issued a code
("i just gave you one, it's still good for a few minutes").
"""
from __future__ import annotations

import logging

from src.providers.ai.types import ToolDef
from src.services.platform_link_service import PlatformLinkService, deep_link
from config import Config
from .base import Tool

logger = logging.getLogger(__name__)

# module-level service, mirroring how other tool modules hold their managers
_links = PlatformLinkService()


async def _link_platform(tool_input: dict, user_uuid: str) -> str:
    code = _links.create_code(user_uuid)
    ttl = Config.LINK_CODE_TTL_MINUTES
    link = deep_link(code)
    lines = [f"link code: {code} (expires in {ttl} minutes)"]
    if link:
        lines.append(f"telegram link: {link}")
        lines.append(
            "tapping the link opens the telegram bot and redeems the code in "
            "one step; pasting the bare code to the bot works too."
        )
    return "\n".join(lines)


LINK_PLATFORM = Tool(
    definition=ToolDef(
        name="link_platform",
        description=(
            "Generate a one-time link code when the user wants to connect "
            "Telegram (or chat with you from another platform). Returns the "
            "code and a tappable telegram link - include BOTH in your reply, "
            f"and mention the code expires in {Config.LINK_CODE_TTL_MINUTES} "
            "minutes."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_link_platform,
)
