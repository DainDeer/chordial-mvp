import logging

from config import Config
from .base import Tool, ToolRegistry
from .intro_tools import COMPLETE_INTRODUCTION, LIST_AVAILABLE_GUIDES
from .memory_tools import SAVE_MEMORY, SEARCH_MEMORIES
from .preference_tools import SET_PREFERENCE

logger = logging.getLogger(__name__)


def build_default_registry() -> ToolRegistry:
    """the live tool set: memory + preferences + introductions, plus notion
    (the dainframe) when a NOTION_API_KEY is configured. adding a capability
    is a register() call here - the agent loop is untouched.

    the two introduction tools (complete_introduction, list_available_guides)
    are registered unconditionally - every helper runs introductions, so they
    always belong in the full registry. chordial's card allowlist is null (the
    full registry), so it gets them for free; specialist cards list an
    explicit `tools:` allowlist and need `complete_introduction` (and
    typically `list_available_guides`) added to it separately - that's a
    persona-card change, not a registry change."""
    registry = ToolRegistry()
    registry.register(SAVE_MEMORY)
    registry.register(SEARCH_MEMORIES)
    registry.register(SET_PREFERENCE)
    registry.register(COMPLETE_INTRODUCTION)
    registry.register(LIST_AVAILABLE_GUIDES)

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
