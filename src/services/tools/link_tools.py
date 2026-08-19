"""the link_platform tool: chordial mints a one-time code (and telegram deep
link) so the user can connect another platform to this same conversation.

deliberately NOT terminal: the tool result must round-trip so the model SEES
the code and weaves it into its reply. record_event stays True - the frozen
action line lets the model know next turn that it already issued a code
("i just gave you one, it's still good for a few minutes").
"""
from __future__ import annotations

import logging

from dainframe.providers.types import ToolDef
from src.services.platform_link_service import PlatformLinkService, deep_link
from config import Config
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool
from src.services.identity import user_of_context

logger = logging.getLogger(__name__)

# module-level service, mirroring how other tool modules hold their managers
_links = PlatformLinkService()


async def _link_platform(tool_input: dict, context: ToolContext) -> str:
    # the dm-only guard retired with the multi-human crew group (phase 7a):
    # every scope is now a private channel with exactly one human - the
    # code's owner is the only person who can see it. the tether's whole
    # link story runs through here from the app's council room.
    user_uuid = user_of_context(context)
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
            "minutes. Only for the person you are talking to; never for "
            "someone else."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_link_platform,
)


async def _web_login(tool_input: dict, context: ToolContext) -> str:
    # imported here so the tool module stays importable when the web view
    # is disabled entirely
    from src.web.auth import login_url, mint_login_code

    # the dm-only guard retired with the multi-human crew group (phase 7a):
    # a login code is a bearer credential, but every scope is now a private
    # channel whose only reader is the code's owner.
    code = mint_login_code(user_of_context(context))
    url = login_url(code)
    ttl = Config.LINK_CODE_TTL_MINUTES
    lines = [f"login code: {code} (expires in {ttl} minutes, single-use)"]
    if url:
        lines.append(f"login link: {url}")
        lines.append(
            "opening the link logs them straight in; typing the code on the "
            "login page works too."
        )
    return "\n".join(lines)


WEB_LOGIN = Tool(
    definition=ToolDef(
        name="web_login",
        description=(
            "Generate a one-time login code when the user wants to open "
            "their focus page (the web view of today's tasks and the "
            "pomodoro bar). Returns the code and a login link - include "
            "BOTH in your reply, and mention the code expires in "
            f"{Config.LINK_CODE_TTL_MINUTES} minutes. Only for the person "
            "you are talking to; never for someone else."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_web_login,
)


async def _link_device(tool_input: dict, context: ToolContext) -> str:
    # imported here so the tool module stays importable if the web server
    # is ever disabled - device linking rides the same aiohttp app
    from src.web.device_auth import mint_device_link_code

    # the dm-only guard retired with the multi-human crew group (phase 7a):
    # every scope is now a private channel with exactly one human.
    code = mint_device_link_code(user_of_context(context))
    ttl = Config.LINK_CODE_TTL_MINUTES
    return (
        f"device link code: {code} (expires in {ttl} minutes, single-use)\n"
        "the user types this code into the chordial desktop app when it "
        "asks to link."
    )


LINK_DEVICE = Tool(
    definition=ToolDef(
        name="link_device",
        description=(
            "Generate a one-time code when the user wants to connect the "
            "chordial desktop app on their computer. Returns the code - "
            "include it in your reply, and mention it expires in "
            f"{Config.LINK_CODE_TTL_MINUTES} minutes. Only for the person "
            "you are talking to; never for someone else."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_link_device,
)
