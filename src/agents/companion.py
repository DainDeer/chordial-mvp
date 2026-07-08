"""the companion: chordial's chat persona, as an agent.

owns everything about HOW chordial speaks - the persona/prompt construction
(PromptService, with its cache-zone layout), the tool surface, and the
tool-call loop on the persona model. the orchestrator just hands it a
briefing and gets back text + the actions it took.
"""
from __future__ import annotations

import logging

from src.agents.base import AgentOutcome, Briefing
from src.services.agent_service import AgentService
from src.services.prompt_service import PromptService
from src.services.tools import ToolRegistry

logger = logging.getLogger(__name__)


class CompanionAgent:
    name = "chordial"

    def __init__(self, agent_service: AgentService, tool_registry: ToolRegistry):
        self.loop = agent_service
        self.registry = tool_registry
        self.prompts = PromptService()

    async def act(self, briefing: Briefing) -> AgentOutcome:
        if briefing.kind == "scheduled_checkin":
            request = await self.prompts.build_scheduled_request(
                conversation_history=briefing.events,
                user_name=briefing.user_name,
                user_uuid=briefing.user_uuid,
                user_timezone=briefing.user_timezone,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
            )
            turn_kind = "scheduled"
        else:
            request = await self.prompts.build_conversation_request(
                conversation_history=briefing.events,
                user_name=briefing.user_name,
                user_uuid=briefing.user_uuid,
                user_timezone=briefing.user_timezone,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
            )
            turn_kind = "conversation"

        result = await self.loop.run(
            request,
            user_uuid=briefing.user_uuid,
            platform=briefing.platform,
            turn_kind=turn_kind,
        )
        return AgentOutcome(
            text=result.text,
            actions=result.actions,
            refused=result.refused,
        )
