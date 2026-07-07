"""tool registry: the plug-in surface for agent capabilities.

each tool pairs a ToolDef (what the model sees) with an async handler (what
runs). handlers receive `user_uuid` injected by the loop - the model never
sees or chooses user identity, which matters the moment there's more than one
user. new capabilities (notion, etc) register here with zero changes to the
agent loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from src.providers.ai.types import ToolCall, ToolDef, ToolResult

logger = logging.getLogger(__name__)

# handler(tool_input, user_uuid) -> human/model-readable result string
ToolHandler = Callable[[dict, str], Awaitable[str]]


@dataclass
class Tool:
    definition: ToolDef
    handler: ToolHandler
    # terminal tools are pure side effects (save a memory, set a preference)
    # whose result the model doesn't need to react to. when a turn's tool calls
    # are all terminal, the agent loop runs them and keeps the reply the model
    # already wrote in that same turn, instead of forcing a second api call that
    # replaces it. see AgentService.run.
    terminal: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDef]:
        return [t.definition for t in self._tools.values()]

    def is_terminal(self, name: str) -> bool:
        """True if this tool is a fire-and-forget side effect. unknown tools are
        treated as non-terminal (safer: they get the normal round-trip)."""
        tool = self._tools.get(name)
        return bool(tool and tool.terminal)

    async def execute(self, call: ToolCall, user_uuid: str) -> ToolResult:
        """run a tool call. errors are returned to the model (is_error=True)
        rather than raised - graceful recovery is part of the UX."""
        tool = self._tools.get(call.name)
        if tool is None:
            logger.warning("model called unknown tool '%s'", call.name)
            return ToolResult(
                tool_call_id=call.id,
                content=f"unknown tool '{call.name}'",
                is_error=True,
            )
        try:
            result = await tool.handler(call.input, user_uuid)
            return ToolResult(tool_call_id=call.id, content=result)
        except Exception as e:
            logger.error("tool '%s' failed: %s", call.name, e)
            return ToolResult(
                tool_call_id=call.id,
                content=f"the {call.name} tool ran into an error: {e}",
                is_error=True,
            )
