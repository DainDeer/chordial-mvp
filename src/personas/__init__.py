"""persona cards: the data behind each helper's voice.

a persona is a frozen YAML card in this directory (one file per id). the card
carries everything that makes a helper itself - its persona_block (the frozen
system prompt, byte-stable so it caches across every request), its tool surface,
how proactive it is, how it's reached on telegram. agents are otherwise
identical; swapping cards swaps who's talking.

cards are loaded and validated once at startup and never mutated. a malformed
card is a startup crash, never a silently-skipped helper - a missing specialist
should be loud.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

_PERSONA_DIR = Path(__file__).resolve().parent

# every key a card must carry. tools may be null (full default registry), but
# the KEY must be present - an omitted `tools` is a malformed card, not a
# shorthand for "everything".
_REQUIRED_FIELDS = (
    "id",
    "archetype",
    "telegram_handle",
    "specialty",
    "proactivity",
    "tools",
    "persona_block",
    "intro_block",
    "intro_question",
)


@dataclass(frozen=True)
class PersonaCard:
    id: str
    archetype: str
    telegram_handle: str
    specialty: str
    proactivity: float
    # None means "the full default registry"; a list is an explicit allowlist,
    # resolved against the registry via ToolRegistry.view at wiring time.
    tools: Optional[list[str]]
    persona_block: str
    intro_block: str
    # the ONE signature question this helper leads its introduction with - the
    # thing it most wants to know about a new person. asked in the helper's own
    # voice, not read verbatim; the guided intro flow builds around it so the
    # conversation has a clear job instead of open-ended "tell me about you".
    intro_question: str


_cache: Optional[dict[str, PersonaCard]] = None


def _load_card(path: Path) -> PersonaCard:
    """parse and validate one card. every failure names the file, so a bad
    card is diagnosable from the crash alone."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"persona card {path.name} is not valid yaml: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(f"persona card {path.name} must be a yaml mapping")

    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ValueError(
            f"persona card {path.name} is missing required field(s): {', '.join(missing)}"
        )

    expected_id = path.stem
    if raw["id"] != expected_id:
        raise ValueError(
            f"persona card {path.name} has id '{raw['id']}' but must match its "
            f"filename ('{expected_id}')"
        )

    tools = raw["tools"]
    if tools is not None and not (
        isinstance(tools, list) and all(isinstance(t, str) for t in tools)
    ):
        raise ValueError(
            f"persona card {path.name}: tools must be null or a list of strings"
        )

    return PersonaCard(
        id=raw["id"],
        archetype=raw["archetype"],
        telegram_handle=raw["telegram_handle"],
        specialty=raw["specialty"],
        proactivity=float(raw["proactivity"]),
        tools=tools,
        persona_block=raw["persona_block"],
        intro_block=raw["intro_block"],
        intro_question=raw["intro_question"],
    )


def load_personas() -> dict[str, PersonaCard]:
    """load and validate every card in this directory, keyed by id. cached
    after the first call - cards are immutable for the process lifetime."""
    global _cache
    if _cache is None:
        cards: dict[str, PersonaCard] = {}
        for path in sorted(_PERSONA_DIR.glob("*.yaml")):
            card = _load_card(path)
            cards[card.id] = card
        _cache = cards
    return _cache
