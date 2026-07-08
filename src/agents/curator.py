"""the curator, as an agent: a thin adapter over MemoryCuratorService.

the service keeps everything that makes it correct (utility-model provider
with thinking=False/effort=None, the planner + validated-executor split, its
own usage accounting). this adapter just gives it the same act(briefing)
face as every other agent, so the orchestrator can dispatch to it without
knowing it works nothing like the companion.
"""
from __future__ import annotations

import logging

from src.agents.base import AgentOutcome, Briefing
from src.services.memory_curator import MemoryCuratorService

logger = logging.getLogger(__name__)


class CuratorAgent:
    name = "curator"

    def __init__(self, service: MemoryCuratorService):
        self.service = service

    async def find_users_needing_curation(self) -> list[str]:
        """discovery stays on the service; surfaced here so the orchestrator
        has one place to ask."""
        return await self.service.find_users_needing_curation()

    async def act(self, briefing: Briefing) -> AgentOutcome:
        result = await self.service.curate_user(briefing.user_uuid)
        # silent agent: no deliverable text, and (v2 decision) curation ops are
        # not recorded as conversation events - AgentTrace already has them
        return AgentOutcome(text=None, errored=bool(result.error))
