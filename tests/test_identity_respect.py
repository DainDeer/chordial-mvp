"""pronouns on the prompt path, and the standing identity guidance.

two things are load-bearing here. first, pronouns must reach system block 2
on EVERY turn kind - a companion that knows them during the introduction and
forgets them by the next conversation is worse than one that never asked.
second, block 0 must be untouched: the guidance deliberately lives in block 2
(like _CREW_AWARENESS) so it covers all seven residents without invalidating the
golden-bytes cache prefix - see test_persona_cards.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_TMP_DB_FD)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB_PATH}")

from src.personas import load_personas  # noqa: E402
from src.services.prompt_service import (  # noqa: E402
    _IDENTITY_RESPECT,
    _INTRO_SHARED_GUIDANCE,
    PromptService,
)
from src.managers.event_log import Event  # noqa: E402


def _history():
    return [Event(author_type="user", author="user", kind="message",
                  content="hi", created_at=datetime(2026, 7, 1, 12, 0))]


def _build(builder_name: str, **kwargs):
    ps = PromptService(persona=load_personas()["vel"],
                       enable_prompt_logging=False)
    builder = getattr(ps, builder_name)

    async def run():
        return await builder(
            conversation_history=_history(),
            user_name="dain",
            user_uuid=None,  # skips the core-memory db lookup
            user_timezone="UTC",
            **kwargs,
        )

    return asyncio.run(run())


@pytest.mark.parametrize("builder", [
    "build_conversation_request",
    "build_introduction_request",
    "build_scheduled_request",
])
def test_pronouns_render_into_block_two_on_every_turn_kind(builder):
    request = _build(builder, user_pronouns="she/her")
    assert "- their pronouns are she/her - use them" in request.system[1].text


@pytest.mark.parametrize("builder", [
    "build_conversation_request",
    "build_introduction_request",
    "build_scheduled_request",
])
def test_identity_guidance_is_standing_not_intro_only(builder):
    """the guidance is about how to hold someone's identity for good, so it
    rides on scheduled check-ins and ordinary turns too - not just the
    introduction where the subject first comes up."""
    request = _build(builder)
    assert _IDENTITY_RESPECT in request.system[1].text


def test_unknown_pronouns_render_as_silence_not_a_guess():
    """None means not yet asked. the absent line is the point: no default, no
    inference from the name sitting one line above it."""
    request = _build("build_conversation_request")
    assert "pronouns are" not in request.system[1].text
    # ...but the standing instruction to ask rather than infer is still there
    assert "never infer pronouns" in request.system[1].text


def test_identity_guidance_stays_out_of_the_golden_cache_prefix():
    request = _build("build_conversation_request", user_pronouns="they/them")
    assert _IDENTITY_RESPECT not in request.system[0].text
    assert "pronouns" not in request.system[0].text


def test_introduction_asks_for_name_and_pronouns_together():
    """asking for pronouns as its own separate beat frames it as the delicate
    question. they go in one breath with the name."""
    assert "pronouns" in _INTRO_SHARED_GUIDANCE
    assert "set_preference(preferred_name=..., pronouns=...)" in _INTRO_SHARED_GUIDANCE
