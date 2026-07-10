"""persona-card infrastructure: loading, validation, and the prompt-cache
survival guarantee.

the load-bearing invariant here is GOLDEN-BYTES: the chordial card's
persona_block must be byte-identical to the retired PERSONA constant, and must
render as system block 0 verbatim. if it drifts by a single byte, every
existing user's warm cache prefix is invalidated on deploy. the literal below
is the frozen source of truth for that check.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# throwaway sqlite db, set before any app module reads Config at import time
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_TMP_DB_FD)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB_PATH}")

from src.personas import PersonaCard, load_personas  # noqa: E402
from src.services.prompt_service import PromptService  # noqa: E402
from src.services.tools.base import Tool, ToolRegistry  # noqa: E402
from src.managers.event_log import Event  # noqa: E402
from src.providers.ai.types import ToolDef  # noqa: E402


# the retired PERSONA constant, verbatim. starts "you are chordial..." and ends
# "...never syrupy or over-eager" with NO trailing newline.
GOLDEN_CHORDIAL_PERSONA = (
    """you are chordial, a personal companion who helps the people you talk with stay on top of their lives - their tasks, their plans, and their wellbeing.

what you do:
- help the user capture and organize what they need to do
- keep track of what matters to them and check in when it's genuinely helpful
- talk through problems, offer encouragement, and be a warm, steady presence

how you work:
- keep replies proportionate: a quick question gets a quick answer; save length and warmth for when it lands
- when the user shares something worth remembering, save it with your memory tools while you reply - saving is a quiet background note, never a substitute for actually responding to them
- when they ask to change how you work (their name, timezone, your style), update it with your tools
- you interact only through this chat - you can't see or do anything outside it

your voice:
- you speak in lowercase, warm and a little playful, like a close friend
- you're never judgmental; you respond naturally to the user's mood
- soft and expressive, but never syrupy or over-eager"""
)

EXPECTED_IDS = {"chordial", "tempo", "aria", "pep", "mochi", "poet"}


# ---------------------------------------------------------------------------
# (a) all six cards load and validate
# ---------------------------------------------------------------------------

def test_all_cards_load_and_validate():
    cards = load_personas()
    assert set(cards) == EXPECTED_IDS
    for card_id, card in cards.items():
        assert isinstance(card, PersonaCard)
        assert card.id == card_id  # id matches the filename/key
        assert card.persona_block.strip()  # non-empty frozen prompt
        assert card.intro_block.strip()
        assert isinstance(card.proactivity, float)
        assert card.tools is None or all(isinstance(t, str) for t in card.tools)


def test_load_personas_is_cached():
    # same object returned each call - cards are immutable for the process
    assert load_personas() is load_personas()


# ---------------------------------------------------------------------------
# (b) GOLDEN-BYTES: chordial's persona_block is byte-identical to the retired
# PERSONA constant. this is the prompt-cache-survival guarantee.
# ---------------------------------------------------------------------------

def test_chordial_persona_block_is_byte_identical_to_retired_constant():
    card = load_personas()["chordial"]
    assert card.persona_block == GOLDEN_CHORDIAL_PERSONA
    assert not card.persona_block.endswith("\n")  # no trailing newline


# ---------------------------------------------------------------------------
# (c) a PromptService built with the chordial card renders system block 0 with
# exactly those bytes.
# ---------------------------------------------------------------------------

def test_prompt_service_renders_chordial_persona_as_system_block_zero():
    ps = PromptService(persona=load_personas()["chordial"], enable_prompt_logging=False)
    history = [Event(author_type="user", author="user", kind="message",
                     content="hi", created_at=datetime(2026, 7, 1, 12, 0))]

    async def run():
        return await ps.build_conversation_request(
            conversation_history=history,
            user_name="dain",
            user_uuid=None,  # skips the core-memory db lookup
            user_timezone="UTC",
        )

    request = asyncio.run(run())
    assert request.system[0].text == GOLDEN_CHORDIAL_PERSONA


# ---------------------------------------------------------------------------
# (d) registry.view() filtering + unknown-name raises at wiring time.
# ---------------------------------------------------------------------------

def _tool(name: str) -> Tool:
    return Tool(
        definition=ToolDef(name=name, description=name, input_schema={"type": "object"}),
        handler=lambda tool_input, user_uuid: None,
    )


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_tool("save_memory"))
    reg.register(_tool("search_memories"))
    reg.register(_tool("create_task"))
    return reg


def test_view_exposes_only_the_named_tools():
    reg = _registry()
    view = reg.view(["save_memory", "search_memories"])
    names = {d.name for d in view.definitions()}
    assert names == {"save_memory", "search_memories"}
    # the filtered registry keeps the same interface
    assert view.should_record("save_memory") is True


def test_view_with_unknown_name_raises():
    reg = _registry()
    with pytest.raises(KeyError):
        reg.view(["save_memory", "not_a_real_tool"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
