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

logger = logging.getLogger(__name__)

# module-level service, mirroring how other tool modules hold their managers
_links = PlatformLinkService()


async def _link_platform(tool_input: dict, context: ToolContext) -> str:
    # same hazard as web_login: a link code redeemed by ANOTHER group member
    # would bind THEIR account to this user. dm-only, default-deny.
    if context.metadata.get("scope") != "dm":
        return (
            "refused: link codes are only issued in a private DM - anyone "
            "in this chat could redeem one as themselves. tell the user to "
            "message you privately and ask again there."
        )
    user_uuid = context.stream_id
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
            "minutes. Only works in a private DM, never in a group chat."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_link_platform,
)


async def _web_login(tool_input: dict, context: ToolContext) -> str:
    # imported here so the tool module stays importable when the web view
    # is disabled entirely
    from src.web.auth import login_url, mint_login_code

    # the tool result lands in the CURRENT reply, and a group-scope reply is
    # delivered to the whole group - a login code is a bearer credential, so
    # anything but a private dm refuses. default-deny: a context that can't
    # prove it's a dm (no scope metadata) is treated as public.
    if context.metadata.get("scope") != "dm":
        return (
            "refused: login codes are only issued in a private DM - anyone "
            "in this chat could use one. tell the user to message you "
            "privately and ask again there."
        )

    code = mint_login_code(context.stream_id)
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
            "you are talking to; never for someone else. Only works in a "
            "private DM, never in a group chat."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_web_login,
)


async def _link_device(tool_input: dict, context: ToolContext) -> str:
    # imported here so the tool module stays importable if the web server
    # is ever disabled - device linking rides the same aiohttp app
    from src.web.device_auth import mint_device_link_code

    # same hazard as the other two: a device code redeemed by another group
    # member enrolls THEIR device against this user. dm-only, default-deny.
    if context.metadata.get("scope") != "dm":
        return (
            "refused: device codes are only issued in a private DM - anyone "
            "in this chat could enroll their own computer as the user. tell "
            "the user to message you privately and ask again there."
        )
    code = mint_device_link_code(context.stream_id)
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
            "you are talking to; never for someone else. Only works in a "
            "private DM, never in a group chat."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    handler=_link_device,
)
