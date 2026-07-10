"""the introduction overhaul (docs/V3_DESIGN.md section 3): the agent-driven
storytelling onboarding that replaces the retired onboarding_service state
machine.

covers the three code-side pieces of the "thin state spine":
- the `complete_introduction` / `list_available_guides` tools
  (src/services/tools/intro_tools.py)
- `PromptService.build_introduction_request` (the volatile-turn framing)
- `HelperAgent.act`'s introduction branch (acting_helper threading)

the prose/pacing itself is the model's job and isn't tested here - only the
state transitions and prompt shape code is responsible for.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base  # noqa: E402
from src.managers.helper_state_manager import HelperStateManager  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.agents.base import Briefing  # noqa: E402
from src.agents.helper import HelperAgent  # noqa: E402
from src.managers.event_log import Event  # noqa: E402
from src.personas import PersonaCard  # noqa: E402
from src.services.prompt_service import PromptService  # noqa: E402
from src.services.tools.base import ToolRegistry  # noqa: E402
from src.services.tools.context import acting_as  # noqa: E402
from src.services.tools.intro_tools import (  # noqa: E402
    COMPLETE_INTRODUCTION, LIST_AVAILABLE_GUIDES,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


async def _make_user(platform="discord", platform_user_id="1") -> str:
    user_uuid, _ = await UserManager().get_or_create_user(platform, platform_user_id, "tester")
    return user_uuid


# --- complete_introduction tool ---------------------------------------------

def test_complete_introduction_accepted_sets_active_and_identity(db):
    user_uuid = run(_make_user())

    with acting_as("chordial"):
        result = run(COMPLETE_INTRODUCTION.handler(
            {"accepted": True, "persona_name": "Ember", "persona_form": "red panda"},
            user_uuid,
        ))

    assert "ember" in result.lower()
    state = run(HelperStateManager().get(user_uuid, "chordial"))
    assert state.status == "active"
    assert state.persona_name == "Ember"
    assert state.persona_form == "red panda"
    assert state.introduced_at is not None


def test_complete_introduction_declined_sets_declined_status(db):
    user_uuid = run(_make_user())

    with acting_as("tempo"):
        result = run(COMPLETE_INTRODUCTION.handler(
            {"accepted": False, "persona_name": None, "persona_form": None},
            user_uuid,
        ))

    assert "declined" in result.lower()
    state = run(HelperStateManager().get(user_uuid, "tempo"))
    assert state.status == "declined"
    assert state.persona_name is None


def test_complete_introduction_attributes_the_acting_helper(db):
    """complete_introduction reads WHICH helper from the tool-loop context, not
    from model input - the model never chooses whose relationship it's
    recording."""
    user_uuid = run(_make_user())

    with acting_as("mochi"):
        run(COMPLETE_INTRODUCTION.handler({"accepted": True}, user_uuid))

    mochi_state = run(HelperStateManager().get(user_uuid, "mochi"))
    chordial_state = run(HelperStateManager().get(user_uuid, "chordial"))
    assert mochi_state.status == "active"
    assert chordial_state.status == "not_met"  # untouched


def test_complete_introduction_blank_strings_normalize_to_none(db):
    user_uuid = run(_make_user())
    with acting_as("chordial"):
        run(COMPLETE_INTRODUCTION.handler(
            {"accepted": True, "persona_name": "  ", "persona_form": ""}, user_uuid,
        ))
    state = run(HelperStateManager().get(user_uuid, "chordial"))
    assert state.persona_name is None
    assert state.persona_form is None


def test_complete_introduction_is_terminal_and_records():
    assert COMPLETE_INTRODUCTION.terminal is True
    assert COMPLETE_INTRODUCTION.record_event is True


# --- list_available_guides tool ---------------------------------------------

def _clear_telegram_usernames(monkeypatch):
    """hermetic env: a developer's real .env may have TELEGRAM_USERNAME_* set,
    which would otherwise leak real deep links into these assertions."""
    from config import Config
    monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", None)
    for hid in ("CHORDIAL", "TEMPO", "ARIA", "PEP", "MOCHI", "POET"):
        monkeypatch.delenv(f"TELEGRAM_USERNAME_{hid}", raising=False)


def test_list_available_guides_excludes_acting_helper_and_lists_the_rest(db, monkeypatch):
    _clear_telegram_usernames(monkeypatch)
    user_uuid = run(_make_user())

    with acting_as("chordial"):
        result = run(LIST_AVAILABLE_GUIDES.handler({}, user_uuid))

    # each guide is one "- <id> (" list line; chordial never lists itself.
    # (substring checks would false-positive on bot usernames like
    # 'chordial_mvp_v3_aria_bot', hence the line-prefix form.)
    assert "- chordial (" not in result
    for helper_id in ("tempo", "aria", "pep", "mochi", "poet"):
        assert f"- {helper_id} (" in result


def test_list_available_guides_deep_link_uses_configured_username_not_card_placeholder(db, monkeypatch):
    """the real, registered @username (config) drives the deep link - never
    the persona card's `telegram_handle` placeholder, which is essentially
    never the name actually available at botfather. no config -> no (wrong)
    link offered, rather than a link to someone else's bot."""
    _clear_telegram_usernames(monkeypatch)
    user_uuid = run(_make_user())

    with acting_as("chordial"):
        unconfigured = run(LIST_AVAILABLE_GUIDES.handler({}, user_uuid))
    assert "t.me/" not in unconfigured

    monkeypatch.setenv("TELEGRAM_USERNAME_TEMPO", "chordial_mvp_v3_tempo_bot")
    with acting_as("chordial"):
        configured = run(LIST_AVAILABLE_GUIDES.handler({}, user_uuid))
    assert "t.me/chordial_mvp_v3_tempo_bot?start=meet" in configured
    assert "t.me/tempo_bot" not in configured  # the card's placeholder, never used


def test_list_available_guides_excludes_active_and_declined(db):
    user_uuid = run(_make_user())
    run(HelperStateManager().complete_introduction(
        user_uuid, "tempo", accepted=True, persona_name="Dash", persona_form="fox"))
    run(HelperStateManager().complete_introduction(
        user_uuid, "mochi", accepted=False))

    with acting_as("chordial"):
        result = run(LIST_AVAILABLE_GUIDES.handler({}, user_uuid))

    assert "tempo" not in result
    assert "mochi" not in result
    assert "aria" in result and "pep" in result and "poet" in result


def test_list_available_guides_is_read_only():
    assert LIST_AVAILABLE_GUIDES.record_event is False


# --- PromptService.build_introduction_request -------------------------------

def _card(**overrides) -> PersonaCard:
    defaults = dict(
        id="chordial",
        archetype="friendly generalist",
        telegram_handle="chordial_bot",
        specialty="the generalist",
        proactivity=0.9,
        tools=None,
        persona_block="you are chordial, frozen persona text.",
        intro_block="you meet them in the forest and learn their name.",
        intro_question="tell me something about yourself you'd want me to remember!",
    )
    defaults.update(overrides)
    return PersonaCard(**defaults)


def test_introduction_request_first_contact_has_no_current_message(db):
    prompts = PromptService(persona=_card(), enable_prompt_logging=False)
    request = run(prompts.build_introduction_request(
        conversation_history=[],
        user_name=None,
        user_uuid=None,
        user_timezone="UTC",
    ))

    # system blocks are the ordinary frozen persona + profile - unaware this
    # is an introduction (cache-stability requirement).
    assert request.system[0].text == "you are chordial, frozen persona text."

    volatile = request.messages[-1].content
    assert "you meet them in the forest and learn their name." in volatile
    assert "representation ritual" in volatile          # the guided flow is present
    assert "tell me something about yourself" in volatile  # the signature question rides along
    assert "begin the introduction now" in volatile


def test_introduction_request_carries_the_personas_signature_question(db):
    """each helper's own intro_question rides in the volatile turn - the guided
    flow builds around one clear question instead of open-ended prompting."""
    prompts = PromptService(
        persona=_card(id="tempo", intro_question="what movement do you love?"),
        enable_prompt_logging=False,
    )
    request = run(prompts.build_introduction_request(
        conversation_history=[], user_name=None, user_uuid=None, user_timezone="UTC",
    ))
    volatile = request.messages[-1].content
    assert "what movement do you love?" in volatile
    assert "signature question" in volatile


def test_introduction_request_folds_in_the_current_user_message(db):
    prompts = PromptService(persona=_card(), enable_prompt_logging=False)
    history = [Event(author_type="user", author="user", kind="message",
                      content="hi there, i wandered in")]

    request = run(prompts.build_introduction_request(
        conversation_history=history,
        user_name=None,
        user_uuid=None,
        user_timezone="UTC",
    ))

    volatile = request.messages[-1].content
    assert volatile.endswith("hi there, i wandered in")
    assert "begin the introduction now" not in volatile


def test_introduction_request_renders_prior_history_for_a_returning_user(db):
    """a returning user meeting a NEW helper: prior events (e.g. this helper's
    own earlier dm turns) render as ordinary history ahead of the intro
    framing, not folded into the volatile turn."""
    prompts = PromptService(persona=_card(id="tempo"), enable_prompt_logging=False)
    history = [
        Event(author_type="user", author="user", kind="message", content="hey tempo"),
        Event(author_type="agent", author="tempo", kind="message", content="hi! good to meet you"),
    ]

    request = run(prompts.build_introduction_request(
        conversation_history=history,
        user_name="Dain",
        user_uuid=None,
        user_timezone="UTC",
    ))

    rendered = [m.content for m in request.messages[:-1]]
    assert any("hey tempo" in c for c in rendered)
    assert any(c == "hi! good to meet you" for c in rendered)


def test_introduction_request_keeps_system_blocks_byte_identical_to_conversation(db):
    """the caching contract: system blocks 1/2 must be indistinguishable from
    an ordinary conversation turn for the same persona/profile."""
    card = _card()
    prompts = PromptService(persona=card, enable_prompt_logging=False)

    convo = run(prompts.build_conversation_request(
        conversation_history=[Event(author_type="user", author="user", kind="message", content="hi")],
        user_name="Dain", user_uuid=None, user_timezone="UTC",
    ))
    intro = run(prompts.build_introduction_request(
        conversation_history=[],
        user_name="Dain", user_uuid=None, user_timezone="UTC",
    ))

    assert [b.text for b in convo.system] == [b.text for b in intro.system]


# --- HelperAgent.act introduction branch ------------------------------------

class _StubLoop:
    """stands in for AgentService: records how it was called."""

    def __init__(self):
        self.calls = []

    async def run(self, request, *, user_uuid, platform, turn_kind, acting_helper="chordial"):
        self.calls.append(dict(
            request=request, user_uuid=user_uuid, platform=platform,
            turn_kind=turn_kind, acting_helper=acting_helper,
        ))

        class _Result:
            text = "hello, i'm tempo"
            actions = []
            refused = False

        return _Result()


def test_helper_agent_introduction_branch_uses_intro_prompt_and_acting_helper(db):
    card = _card(id="tempo", telegram_handle="tempo_bot")
    loop = _StubLoop()
    registry = ToolRegistry()
    registry.register(COMPLETE_INTRODUCTION)

    agent = HelperAgent(card, loop, registry)
    briefing = Briefing(
        kind="introduction",
        user_uuid="u1",
        platform="telegram",
        user_name=None,
        user_timezone="UTC",
        events=[],
    )

    outcome = run(agent.act(briefing))

    assert outcome.text == "hello, i'm tempo"
    assert len(loop.calls) == 1
    call = loop.calls[0]
    assert call["turn_kind"] == "introduction"
    assert call["acting_helper"] == "tempo"
    # the request carries this persona's intro_block in its volatile turn
    assert "you meet them in the forest" in call["request"].messages[-1].content


def test_helper_agent_threads_acting_helper_on_ordinary_turns_too(db):
    """not just introductions - every loop.run call from HelperAgent must
    attribute the right helper, so save_memory/complete_introduction always
    work regardless of activation kind."""
    card = _card(id="aria", telegram_handle="aria_bot")
    loop = _StubLoop()
    registry = ToolRegistry()

    agent = HelperAgent(card, loop, registry)
    briefing = Briefing(
        kind="user_message", user_uuid="u1", platform="telegram",
        user_name="Dain", user_timezone="UTC", events=[],
    )
    run(agent.act(briefing))

    assert loop.calls[0]["acting_helper"] == "aria"
    assert loop.calls[0]["turn_kind"] == "conversation"
