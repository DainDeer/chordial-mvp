"""the agent contract: what the orchestrator briefs, and what comes back.

an Agent is a named actor. the orchestrator decides WHO acts and hands them a
Briefing (the necessary info for this activation); the agent owns HOW - its
persona, its prompt construction, its model, its tools. two very different
shapes already fit this one interface: the companion (persona model, tool
loop, user-facing text) and the curator (utility model, one-shot planner,
silent). v3's cast of characters are just more Agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable

from src.managers.event_log import Event
from src.services.agent_service import ExecutedAction


@dataclass
class Briefing:
    """everything the orchestrator hands an agent for one activation."""
    kind: str                        # 'user_message' | 'scheduled_checkin' | 'curation'
    user_uuid: str
    platform: Optional[str] = None
    user_name: Optional[str] = None
    user_timezone: str = "UTC"
    # recent event window; for kind='user_message' the last item is the
    # just-received user message (the volatile current turn)
    events: List[Event] = field(default_factory=list)
    ambient_context: Optional[str] = None
    # future: the check-in gate's "why now" (woven into scheduled prompts)
    reason: Optional[str] = None
    extras: dict = field(default_factory=dict)


@dataclass
class AgentOutcome:
    """what an activation produced. text=None means the agent acted silently
    (the curator) or produced nothing deliverable."""
    text: Optional[str] = None
    actions: List[ExecutedAction] = field(default_factory=list)
    refused: bool = False
    errored: bool = False


@runtime_checkable
class Agent(Protocol):
    name: str                        # event-log author id: 'chordial', 'curator', ...

    async def act(self, briefing: Briefing) -> AgentOutcome: ...
