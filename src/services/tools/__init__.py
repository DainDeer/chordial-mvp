import logging

from config import Config
# the registry machinery lives in the dainframe now; re-exported here so
# chordial code keeps one import home for its tool surface
from dainframe.tools.registry import Tool, ToolRegistry
from .intro_tools import COMPLETE_INTRODUCTION, LIST_AVAILABLE_GUIDES
from .memory_tools import SAVE_MEMORY, SEARCH_MEMORIES
from .preference_tools import SET_PREFERENCE

logger = logging.getLogger(__name__)


def build_default_registry() -> ToolRegistry:
    """the live tool set: memory + preferences + introductions + the native
    workspace. adding a capability is a register() call here - the agent loop
    is untouched.

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

    # the native workspace surface: tasks/plans/cycles (core) plus the v3
    # additions (goals/wins/check-ins/notes/occasions). all in-db, always on.
    from .workspace_tools import WORKSPACE_CORE_TOOLS, WORKSPACE_EXTRA_TOOLS
    for tool in WORKSPACE_EXTRA_TOOLS:
        registry.register(tool)
    for tool in WORKSPACE_CORE_TOOLS:
        registry.register(tool)
    logger.info("workspace tools enabled (%d registered)",
                len(WORKSPACE_CORE_TOOLS) + len(WORKSPACE_EXTRA_TOOLS))

    if Config.telegram_linking_enabled():
        # config-stable gating (like notion): the tool's bytes only change at
        # deploy time, so the prompt cache is unaffected.
        from .link_tools import LINK_PLATFORM
        registry.register(LINK_PLATFORM)
        logger.info("platform linking tool enabled")

    if Config.web_auth_enabled():
        # same config-stable gating: the tool exists exactly when the page
        # is public enough to need logging into
        from .link_tools import WEB_LOGIN
        registry.register(WEB_LOGIN)
        logger.info("web login tool enabled (%s)", Config.WEB_PUBLIC_URL)

    return registry


__all__ = ["Tool", "ToolRegistry", "build_default_registry"]
