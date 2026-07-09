import logging

from config import Config
from .base import Tool, ToolRegistry
from .memory_tools import SAVE_MEMORY, SEARCH_MEMORIES
from .preference_tools import SET_PREFERENCE

logger = logging.getLogger(__name__)


def build_default_registry() -> ToolRegistry:
    """the live tool set: memory + preferences, plus notion (the dainframe)
    when a NOTION_API_KEY is configured. adding a capability is a register()
    call here - the agent loop is untouched."""
    registry = ToolRegistry()
    registry.register(SAVE_MEMORY)
    registry.register(SEARCH_MEMORIES)
    registry.register(SET_PREFERENCE)

    if Config.notion_enabled():
        # imported lazily so the app runs (and tests pass) without a notion key.
        from .notion_tools import NOTION_TOOLS
        for tool in NOTION_TOOLS:
            registry.register(tool)
        logger.info("notion tools enabled (%d registered)", len(NOTION_TOOLS))
    else:
        logger.info("notion tools disabled (no NOTION_API_KEY set)")

    if Config.telegram_linking_enabled():
        # config-stable gating (like notion): the tool's bytes only change at
        # deploy time, so the prompt cache is unaffected.
        from .link_tools import LINK_PLATFORM
        registry.register(LINK_PLATFORM)
        logger.info("platform linking tool enabled")

    return registry


__all__ = ["Tool", "ToolRegistry", "build_default_registry"]
