"""agents: named actors the orchestrator briefs.

the contract lives in base (Briefing / AgentOutcome / Agent); the cast so far
is the companion (chordial's chat persona) and the curator (silent memory
hygiene). v3 personas join by implementing act(briefing) and registering with
the orchestrator - nothing else changes.
"""
from .base import Agent, AgentOutcome, Briefing
from .companion import CompanionAgent
from .curator import CuratorAgent

__all__ = ["Agent", "AgentOutcome", "Briefing", "CompanionAgent", "CuratorAgent"]
