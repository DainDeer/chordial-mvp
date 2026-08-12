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

# the council chair: the front door, the default speaker, the voice of the
# house (ROOMS_DESIGN.md §3). exactly one card carries `chair: true`, and it
# must be this one - a constant so low-level modules (router defaults, config
# fallbacks) can name the chair without loading yaml, with load_personas()
# validating that the cards agree.
CHAIR_ID = "vel"

# every key a card must carry. tools may be null (full default registry), but
# the KEY must be present - an omitted `tools` is a malformed card, not a
# shorthand for "everything".
_REQUIRED_FIELDS = (
    "id",
    "species",
    "emoji",
    "lane",
    "archetype",
    "telegram_handle",
    "specialty",
    "proactivity",
    "tools",
    "speak_when",
    "default_rooms",
    "persona_block",
    "intro_block",
    "intro_question",
)


@dataclass(frozen=True)
class PersonaCard:
    id: str
    # the authored identity (ROOMS_DESIGN.md §3): the council ships with
    # names and species now - "authored defaults, personally reshapeable".
    # a user's rename/reshape lives in helper_states + a private core
    # memory, never here.
    species: str
    emoji: str
    # the machine-readable lane id ("productivity", "movement", ...) - the
    # speaking-policy router keys on this; `specialty` stays the prose.
    lane: str
    archetype: str
    telegram_handle: str
    specialty: str
    proactivity: float
    # None means "the full default registry"; a list is an explicit allowlist,
    # resolved against the registry via ToolRegistry.view at wiring time.
    tools: Optional[list[str]]
    # deterministic speaking-policy triggers (ROOMS_DESIGN.md §3): when this
    # helper is a candidate to speak unprompted. consumed by the phase-3
    # routing spine; authored here so a card is complete in one file.
    speak_when: list[str]
    # room types this helper is present in by default.
    default_rooms: list[str]
    persona_block: str
    intro_block: str
    # the ONE signature question this helper leads its introduction with - the
    # thing it most wants to know about a new person. asked in the helper's own
    # voice, not read verbatim; the guided intro flow builds around it so the
    # conversation has a clear job instead of open-ended "tell me about you".
    intro_question: str
    # the council chair holds the room and speaks for the house; the director
    # falls back to the chair on every conversational path.
    chair: bool = False


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

    for field in ("speak_when", "default_rooms"):
        value = raw[field]
        if not (
            isinstance(value, list)
            and value
            and all(isinstance(v, str) for v in value)
        ):
            raise ValueError(
                f"persona card {path.name}: {field} must be a non-empty "
                f"list of strings"
            )

    chair = raw.get("chair", False)
    if not isinstance(chair, bool):
        raise ValueError(f"persona card {path.name}: chair must be a boolean")

    return PersonaCard(
        id=raw["id"],
        species=raw["species"],
        emoji=raw["emoji"],
        lane=raw["lane"],
        archetype=raw["archetype"],
        telegram_handle=raw["telegram_handle"],
        specialty=raw["specialty"],
        proactivity=float(raw["proactivity"]),
        tools=tools,
        speak_when=list(raw["speak_when"]),
        default_rooms=list(raw["default_rooms"]),
        persona_block=raw["persona_block"],
        intro_block=raw["intro_block"],
        intro_question=raw["intro_question"],
        chair=chair,
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
        chairs = sorted(c.id for c in cards.values() if c.chair)
        if chairs != [CHAIR_ID]:
            # the chair is load-bearing (director fallback, front door,
            # telegram back-compat) - a missing or contested chair is a
            # startup crash, never a silently-wrong default speaker
            raise ValueError(
                f"exactly one persona card must set chair: true and it must "
                f"be '{CHAIR_ID}' (found: {chairs or 'none'})"
            )
        _cache = cards
    return _cache


def validate_enabled(enabled_ids) -> None:
    """startup validation of ENABLED_HELPERS against the cards: every id must
    be a known card, and the chair must be among them - the director casts
    fallback lines for the chair on every conversational path, so a
    deployment without it would route messages to an agent that was never
    constructed. crashes loudly at startup, never quietly mid-conversation."""
    cards = load_personas()
    unknown = [h for h in enabled_ids if h not in cards]
    if unknown:
        raise RuntimeError(
            f"ENABLED_HELPERS lists unknown persona(s) "
            f"{', '.join(sorted(unknown))} (known: {', '.join(sorted(cards))})"
        )
    if CHAIR_ID not in enabled_ids:
        raise RuntimeError(
            f"ENABLED_HELPERS must include the chair '{CHAIR_ID}' - the "
            f"director falls back to the chair on every conversational path, "
            f"so a deployment without it cannot route messages"
        )
