"""agents: named actors the orchestrator briefs.

the contract lives in base (Briefing / AgentOutcome / Agent); the cast is the
helpers (each a persona's chat agent, driven by a PersonaCard) and the curator
(silent memory hygiene). a new persona joins by dropping a card in src/personas
and enabling its id - the HelperAgent is the same for all of them.
"""
from .base import Agent, AgentOutcome, Briefing
from .helper import HelperAgent
from .curator import CuratorAgent

__all__ = ["Agent", "AgentOutcome", "Briefing", "HelperAgent", "CuratorAgent"]
