from .base import Tool, ToolRegistry
from .memory_tools import SAVE_MEMORY, SEARCH_MEMORIES
from .preference_tools import SET_PREFERENCE


def build_default_registry() -> ToolRegistry:
    """the phase-1 tool set: memory + preferences. notion and scheduling tools
    register here in later phases with no change to the agent loop."""
    registry = ToolRegistry()
    registry.register(SAVE_MEMORY)
    registry.register(SEARCH_MEMORIES)
    registry.register(SET_PREFERENCE)
    return registry


__all__ = ["Tool", "ToolRegistry", "build_default_registry"]
