"""the helper: a persona's chat agent.

owns everything about HOW one persona speaks - the persona/prompt construction
(PromptService, with its cache-zone layout), the tool surface, and the
tool-call loop on the persona model. the engine just hands it a briefing and
gets back text + the actions it took.

the persona itself lives in a PersonaCard (id, voice, tool allowlist); the agent
is otherwise identical from one helper to the next. swapping cards swaps who's
talking. `card.tools` None means the full registry; a list is an explicit
allowlist, resolved to a filtered view at construction (a typo raises here, not
mid-chat).

the briefing is the dainframe's: the user's identity rides in extras
(user_name/user_timezone from ChordialContext), the stream id IS the user
uuid, and the event window arrives as library events - converted at this
boundary to chordial Events so PromptService renders byte-identical prompts.
"""
from __future__ import annotations

import logging

from dainframe.core import AgentOutcome, Briefing
from dainframe.loop.agent_loop import AgentLoop
from dainframe.tools.context import ToolContext

from src.managers.event_log import Event
from src.personas import PersonaCard
from src.services.prompt_service import PromptService
from src.services.tools import ToolRegistry

logger = logging.getLogger(__name__)


class HelperAgent:
    def __init__(self, card: PersonaCard, agent_service: AgentLoop, tool_registry: ToolRegistry):
        self.card = card
        self.name = card.id
        self.loop = agent_service
        self.registry = tool_registry if card.tools is None else tool_registry.view(card.tools)
        self.prompts = PromptService(persona=card)

    async def act(self, briefing: Briefing) -> AgentOutcome:
        user_uuid = briefing.stream_id
        user_name = briefing.extras.get("user_name")
        user_timezone = briefing.extras.get("user_timezone") or "UTC"
        history = [Event.from_dainframe(e) for e in briefing.events]

        if briefing.kind == "introduction":
            request = await self.prompts.build_introduction_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
            )
            turn_kind = "introduction"
        elif briefing.kind == "scheduled_checkin":
            request = await self.prompts.build_scheduled_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
            )
            turn_kind = "scheduled"
        else:
            request = await self.prompts.build_conversation_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
            )
            turn_kind = "conversation"

        result = await self.loop.run(
            request,
            # the engine's activation id ties this run's tool actions to the
            # turn that produced them
            context=ToolContext(
                stream_id=user_uuid,
                activation_id=briefing.activation_id,
                actor=self.name,
            ),
            platform=briefing.platform,
            turn_kind=turn_kind,
        )
        return AgentOutcome(
            text=result.text,
            actions=tuple(result.actions),
            refused=result.refused,
        )
