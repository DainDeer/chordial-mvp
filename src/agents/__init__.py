"""agents: named actors the engine briefs.

the contract (Agent / Briefing / AgentOutcome) is the dainframe's, re-exported
here so chordial code keeps one import home; the cast is the helpers (each a
persona's chat agent, driven by a PersonaCard) and the curator (silent memory
hygiene). a new persona joins by dropping a card in src/personas and enabling
its id - the HelperAgent is the same for all of them.
"""
from dainframe.core import Agent, AgentOutcome, Briefing

from .helper import HelperAgent
from .curator import CuratorAgent

__all__ = ["Agent", "AgentOutcome", "Briefing", "HelperAgent", "CuratorAgent"]
