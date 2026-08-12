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
from src.agents import Briefing  # noqa: E402
from src.agents.helper import HelperAgent  # noqa: E402
from src.managers.event_log import Event  # noqa: E402
from src.personas import PersonaCard, load_personas  # noqa: E402
from src.services.prompt_service import PromptService  # noqa: E402
from src.services.tools import ToolRegistry  # noqa: E402
from dainframe.tools.context import ToolContext  # noqa: E402
from config import Config  # noqa: E402


def _ctx(user_uuid: str, actor: str) -> ToolContext:
    """the tool-loop context AgentLoop would bind: which stream, which actor.
    the model never chooses whose relationship it's recording - the identity
    arrives on the context."""
    return ToolContext(stream_id=user_uuid, activation_id="test-act", actor=actor)
from src.services.tools.intro_tools import (  # noqa: E402
    COMPLETE_INTRODUCTION,
    LIST_AVAILABLE_GUIDES,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


async def _make_user(platform="discord", platform_user_id="1") -> str:
    user_uuid, _ = await UserManager().get_or_create_user(
        platform, platform_user_id, "tester"
    )
    return user_uuid


# --- private identity is invisible to siblings (the core-memory privacy filter) ---


def test_helper_identity_memory_is_private_to_that_helper(db):
    """a helper's own form/name, saved core+private, renders as a core memory
    in ITS OWN prompt but never in a sibling's - the fix for one helper reading
    another's 'you are a red panda' identity as its own."""
    from src.managers.memories_manager import MemoriesManager, MemoryType, MemorySource

    mem = MemoriesManager()
    user_uuid = run(_make_user())

    # vel's private identity + a shared fact about the user
    run(
        mem.upsert_memory(
            user_uuid,
            "to dain, you are matcha the red panda",
            MemoryType.FACT,
            MemorySource.AI_INFERRED,
            core=True,
            created_by="vel",
            visibility="private",
        )
    )
    run(
        mem.upsert_memory(
            user_uuid,
            "dain is a pink deer building vel",
            MemoryType.FACT,
            MemorySource.AI_INFERRED,
            core=True,
            created_by="vel",
            visibility="shared",
        )
    )

    def profile_for(helper_id):
        card = _card(id=helper_id)
        req = run(
            PromptService(
                persona=card, enable_prompt_logging=False
            ).build_conversation_request(
                conversation_history=[
                    Event(
                        author_type="user", author="user", kind="message", content="hi"
                    )
                ],
                user_name="Dain",
                user_uuid=user_uuid,
                user_timezone="UTC",
            )
        )
        return req.system[1].text

    chordial_profile = profile_for("vel")
    tempo_profile = profile_for("skip")

    assert "matcha the red panda" in chordial_profile  # its own identity
    assert "matcha the red panda" not in tempo_profile  # NOT leaked to skip
    assert (
        "pink deer" in chordial_profile and "pink deer" in tempo_profile
    )  # shared fact everywhere


# --- complete_introduction tool ---------------------------------------------


def test_complete_introduction_accepted_sets_active_and_identity(db):
    user_uuid = run(_make_user())

    result = run(
        COMPLETE_INTRODUCTION.handler(
            {
                "accepted": True,
                "persona_name": "Ember",
                "persona_form": "red panda",
            },
            _ctx(user_uuid, "vel"),
        )
    )

    assert "ember" in result.lower()
    state = run(HelperStateManager().get(user_uuid, "vel"))
    assert state.status == "active"
    assert state.persona_name == "Ember"
    assert state.persona_form == "red panda"
    assert state.introduced_at is not None


def test_complete_introduction_declined_sets_declined_status(db):
    user_uuid = run(_make_user())

    result = run(
        COMPLETE_INTRODUCTION.handler(
            {"accepted": False, "persona_name": None, "persona_form": None},
            _ctx(user_uuid, "skip"),
        )
    )

    assert "declined" in result.lower()
    state = run(HelperStateManager().get(user_uuid, "skip"))
    assert state.status == "declined"
    assert state.persona_name is None


def test_complete_introduction_attributes_the_acting_helper(db):
    """complete_introduction reads WHICH helper from the tool-loop context, not
    from model input - the model never chooses whose relationship it's
    recording."""
    user_uuid = run(_make_user())

    run(COMPLETE_INTRODUCTION.handler({"accepted": True}, _ctx(user_uuid, "mabel")))

    mochi_state = run(HelperStateManager().get(user_uuid, "mabel"))
    chordial_state = run(HelperStateManager().get(user_uuid, "vel"))
    assert mochi_state.status == "active"
    assert chordial_state.status == "not_met"  # untouched


def test_complete_introduction_blank_strings_normalize_to_none(db):
    user_uuid = run(_make_user())
    run(
        COMPLETE_INTRODUCTION.handler(
            {"accepted": True, "persona_name": "  ", "persona_form": ""},
            _ctx(user_uuid, "vel"),
        )
    )
    state = run(HelperStateManager().get(user_uuid, "vel"))
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
    for hid in ("VEL", "PIP", "SKIP", "REMY", "MABEL", "JUNIPER", "EDWIN"):
        monkeypatch.delenv(f"TELEGRAM_USERNAME_{hid}", raising=False)


def test_list_available_guides_excludes_acting_helper_and_lists_the_rest(
    db, monkeypatch
):
    _clear_telegram_usernames(monkeypatch)
    user_uuid = run(_make_user())

    result = run(LIST_AVAILABLE_GUIDES.handler({}, _ctx(user_uuid, "vel")))

    # each guide is one "<id> the <species>" list line; vel never lists
    # itself. (substring checks on the bare id would false-positive on bot
    # usernames like 'chordial_mvp_v3_pip_bot', hence the fuller form.)
    assert " vel the " not in result
    for helper_id, species in (("skip", "bunny"), ("juniper", "fox"),
                               ("pip", "squirrel"), ("mabel", "bear"),
                               ("remy", "raccoon"), ("edwin", "owl")):
        assert f" {helper_id} the {species} - " in result


def test_list_available_guides_deep_link_uses_configured_username_not_card_placeholder(
    db, monkeypatch
):
    """the real, registered @username (config) drives the deep link - never
    the persona card's `telegram_handle` placeholder, which is essentially
    never the name actually available at botfather. no config -> no (wrong)
    link offered, rather than a link to someone else's bot."""
    _clear_telegram_usernames(monkeypatch)
    user_uuid = run(_make_user())

    unconfigured = run(LIST_AVAILABLE_GUIDES.handler({}, _ctx(user_uuid, "vel")))
    assert "t.me/" not in unconfigured

    monkeypatch.setenv("TELEGRAM_USERNAME_SKIP", "chordial_mvp_v3_skip_bot")
    configured = run(LIST_AVAILABLE_GUIDES.handler({}, _ctx(user_uuid, "vel")))
    assert "t.me/chordial_mvp_v3_skip_bot?start=meet" in configured
    assert "t.me/skip_bot" not in configured  # the card's placeholder, never used


def test_list_available_guides_excludes_active_and_declined(db):
    user_uuid = run(_make_user())
    run(
        HelperStateManager().complete_introduction(
            user_uuid, "skip", accepted=True, persona_name="Dash", persona_form="fox"
        )
    )
    run(HelperStateManager().complete_introduction(user_uuid, "mabel", accepted=False))

    result = run(LIST_AVAILABLE_GUIDES.handler({}, _ctx(user_uuid, "vel")))

    assert "skip" not in result
    assert "mabel" not in result
    assert "juniper" in result and "pip" in result and "remy" in result


def test_list_available_guides_is_read_only():
    """record_event stays False on purpose: a recorded roster freezes into
    cache-stable history, and the model answers the NEXT roster question from
    that stale copy instead of calling again. auditability rides on the
    handler's log line + agent_traces.tool_trace instead."""
    assert LIST_AVAILABLE_GUIDES.record_event is False


def test_list_available_guides_logs_what_it_handed_back(db, monkeypatch, caplog):
    """the audit trail that replaces record_event. this tool's calls were
    invisible in the event log, so 'did it even ask?' was unanswerable from
    the DB alone after a helper confabulated a roster (2026-08-05)."""
    _clear_telegram_usernames(monkeypatch)
    monkeypatch.setenv("TELEGRAM_USERNAME_MABEL", "chordial_mvp_v3_mabel_bot")
    user_uuid = run(_make_user())

    with caplog.at_level("INFO", logger="src.services.tools.intro_tools"):
        run(LIST_AVAILABLE_GUIDES.handler({}, _ctx(user_uuid, "vel")))

    line = next(r.getMessage() for r in caplog.records if "list_available_guides" in r.getMessage())
    assert "actor=vel" in line and user_uuid in line
    assert "mabel (no link)" not in line  # configured: a link went out
    assert "skip (no link)" in line  # unconfigured: flagged, still offered


def test_list_available_guides_description_covers_mid_conversation_asks():
    """the description used to scope itself to 'after your own introduction
    wraps up', so a mid-conversation "introduce me to the other companions?"
    didn't match it and the helper answered from imagination instead."""
    text = LIST_AVAILABLE_GUIDES.definition.description.lower()
    for phrasing in ("companions", "guides", "who else"):
        assert phrasing in text
    assert "never" in text  # the don't-invent-a-link rule


# --- standing crew awareness in the system prompt ---------------------------


def _block2(card, **overrides) -> str:
    kwargs = dict(
        conversation_history=[
            Event(author_type="user", author="user", kind="message", content="hi")
        ],
        user_name="Dain",
        user_uuid=None,
        user_timezone="UTC",
    )
    kwargs.update(overrides)
    prompts = PromptService(persona=card, enable_prompt_logging=False)
    return run(prompts.build_conversation_request(**kwargs)).system[1].text


def test_conversation_turns_carry_crew_awareness(monkeypatch):
    """the actual 2026-08-05 bug: on an ordinary conversation turn nothing in
    the prompt said a crew existed or that the roster had to be fetched - that
    instruction lived only in the introduction flow. asked about the other
    helpers, vel invented four of them, with wrong specialties and dead
    links, without ever calling the tool."""
    monkeypatch.setattr(Config, "ENABLED_HELPERS", ["vel", "skip", "mabel"])

    text = _block2(_card())

    assert "list_available_guides" in text
    assert "rest of the council" in text


def test_crew_awareness_reaches_scheduled_and_introduction_turns(monkeypatch):
    """it lives in the shared system block, so no turn kind is exempt - a
    proactive check-in can be asked about the crew just as easily."""
    monkeypatch.setattr(Config, "ENABLED_HELPERS", ["vel", "skip"])
    prompts = PromptService(persona=_card(), enable_prompt_logging=False)

    for build in (prompts.build_scheduled_request, prompts.build_introduction_request):
        request = run(
            build(
                conversation_history=[],
                user_name="Dain",
                user_uuid=None,
                user_timezone="UTC",
            )
        )
        assert "list_available_guides" in request.system[1].text


def test_no_crew_awareness_in_a_solo_deployment(monkeypatch):
    """one enabled helper has no crew to offer; promising one would be its own
    invented roster (.env.dev runs exactly this shape)."""
    monkeypatch.setattr(Config, "ENABLED_HELPERS", ["vel"])

    assert "list_available_guides" not in _block2(_card())


def test_no_crew_awareness_when_the_card_cannot_call_the_tool(monkeypatch):
    """guidance and tool surface stay in sync: a card whose allowlist omits the
    tool is never told to call it."""
    monkeypatch.setattr(Config, "ENABLED_HELPERS", ["vel", "skip"])

    without = _card(id="skip", tools=["save_memory"])
    with_it = _card(id="skip", tools=["save_memory", "list_available_guides"])

    assert "list_available_guides" not in _block2(without)
    assert "list_available_guides" in _block2(with_it)


# --- PromptService.build_introduction_request -------------------------------


def test_live_persona_intros_are_sceneless_and_excited(db):
    # 2026-08 voice overhaul: no narrative staging (the old forest frame read
    # as melancholy and seeded every conversation's tone) - intros open on
    # personality and excitement alone.
    cards = load_personas()
    for card in cards.values():
        lowered = card.intro_block.lower()
        assert "forest" not in lowered
        # authored cast: every helper shows up as itself, no staged reveal
        assert "arrive as yourself" in lowered

    request = run(
        PromptService(cards["vel"], enable_prompt_logging=False).build_introduction_request(
            conversation_history=[],
            user_name=None,
            user_uuid=None,
            user_timezone="UTC",
        )
    )
    guidance = request.messages[-1].content.lower()
    assert "this is a first meeting" in guidance
    assert "no scene-setting" in guidance
    assert "forest" not in guidance


def _card(**overrides) -> PersonaCard:
    defaults = dict(
        id="vel",
        species="deer",
        emoji="🦌",
        lane="presence",
        archetype="friendly generalist",
        telegram_handle="vel_bot",
        specialty="the generalist",
        proactivity=0.9,
        tools=None,
        speak_when=["directly_addressed"],
        default_rooms=["daily"],
        persona_block="you are vel, frozen persona text.",
        intro_block="you bounce up to greet them and learn their name.",
        intro_question="tell me something about yourself you'd want me to remember!",
        chair=True,
    )
    defaults.update(overrides)
    return PersonaCard(**defaults)


def test_introduction_request_first_contact_has_no_current_message(db):
    prompts = PromptService(persona=_card(), enable_prompt_logging=False)
    request = run(
        prompts.build_introduction_request(
            conversation_history=[],
            user_name=None,
            user_uuid=None,
            user_timezone="UTC",
        )
    )

    # system blocks are the ordinary frozen persona + profile - unaware this
    # is an introduction (cache-stability requirement).
    assert request.system[0].text == "you are vel, frozen persona text."

    volatile = request.messages[-1].content
    assert "you bounce up to greet them and learn their name." in volatile
    assert "make me yours" in volatile  # the guided flow is present
    assert (
        "tell me something about yourself" in volatile
    )  # the signature question rides along
    assert "begin the introduction now" in volatile


def test_introduction_request_carries_the_personas_signature_question(db):
    """each helper's own intro_question rides in the volatile turn - the guided
    flow builds around one clear question instead of open-ended prompting."""
    prompts = PromptService(
        persona=_card(id="skip", intro_question="what movement do you love?"),
        enable_prompt_logging=False,
    )
    request = run(
        prompts.build_introduction_request(
            conversation_history=[],
            user_name=None,
            user_uuid=None,
            user_timezone="UTC",
        )
    )
    volatile = request.messages[-1].content
    assert "what movement do you love?" in volatile
    assert "signature question" in volatile


def test_introduction_request_folds_in_the_current_user_message(db):
    prompts = PromptService(persona=_card(), enable_prompt_logging=False)
    history = [
        Event(
            author_type="user",
            author="user",
            kind="message",
            content="hi there, i wandered in",
        )
    ]

    request = run(
        prompts.build_introduction_request(
            conversation_history=history,
            user_name=None,
            user_uuid=None,
            user_timezone="UTC",
        )
    )

    volatile = request.messages[-1].content
    assert volatile.endswith("hi there, i wandered in")
    assert "begin the introduction now" not in volatile


def test_introduction_request_renders_prior_history_for_a_returning_user(db):
    """a returning user meeting a NEW helper: prior events (e.g. this helper's
    own earlier dm turns) render as ordinary history ahead of the intro
    framing, not folded into the volatile turn."""
    prompts = PromptService(persona=_card(id="skip"), enable_prompt_logging=False)
    history = [
        Event(author_type="user", author="user", kind="message", content="hey skip"),
        Event(
            author_type="agent",
            author="skip",
            kind="message",
            content="hi! good to meet you",
        ),
    ]

    request = run(
        prompts.build_introduction_request(
            conversation_history=history,
            user_name="Dain",
            user_uuid=None,
            user_timezone="UTC",
        )
    )

    rendered = [m.content for m in request.messages[:-1]]
    assert any("hey skip" in c for c in rendered)
    assert any(c == "hi! good to meet you" for c in rendered)


def test_introduction_request_keeps_system_blocks_byte_identical_to_conversation(db):
    """the caching contract: system blocks 1/2 must be indistinguishable from
    an ordinary conversation turn for the same persona/profile."""
    card = _card()
    prompts = PromptService(persona=card, enable_prompt_logging=False)

    convo = run(
        prompts.build_conversation_request(
            conversation_history=[
                Event(author_type="user", author="user", kind="message", content="hi")
            ],
            user_name="Dain",
            user_uuid=None,
            user_timezone="UTC",
        )
    )
    intro = run(
        prompts.build_introduction_request(
            conversation_history=[],
            user_name="Dain",
            user_uuid=None,
            user_timezone="UTC",
        )
    )

    assert [b.text for b in convo.system] == [b.text for b in intro.system]


# --- HelperAgent.act introduction branch ------------------------------------


class _StubLoop:
    """stands in for the AgentLoop: records how it was called."""

    def __init__(self):
        self.calls = []

    async def run(self, request, *, context, platform, turn_kind, hints=None):
        self.calls.append(
            dict(
                request=request,
                user_uuid=context.stream_id,
                platform=platform,
                turn_kind=turn_kind,
                acting_helper=context.actor,
                hints=hints,
            )
        )

        class _Result:
            text = "hello, i'm skip"
            actions = []
            refused = False

        return _Result()


def test_helper_agent_introduction_branch_uses_intro_prompt_and_acting_helper(db):
    card = _card(id="skip", telegram_handle="tempo_bot")
    loop = _StubLoop()
    registry = ToolRegistry()
    registry.register(COMPLETE_INTRODUCTION)

    agent = HelperAgent(card, loop, registry)
    briefing = Briefing(
        kind="introduction",
        stream_id="u1",
        activation_id="act-test",
        platform="telegram",
        events=(),
        extras={"user_name": None, "user_timezone": "UTC"},
    )

    outcome = run(agent.act(briefing))

    assert outcome.text == "hello, i'm skip"
    assert len(loop.calls) == 1
    call = loop.calls[0]
    assert call["turn_kind"] == "introduction"
    assert call["acting_helper"] == "skip"
    # the request carries this persona's intro_block in its volatile turn
    assert "you bounce up to greet them" in call["request"].messages[-1].content


def test_helper_agent_threads_acting_helper_on_ordinary_turns_too(db):
    """not just introductions - every loop.run call from HelperAgent must
    attribute the right helper, so save_memory/complete_introduction always
    work regardless of activation kind."""
    card = _card(id="juniper", telegram_handle="aria_bot")
    loop = _StubLoop()
    registry = ToolRegistry()

    agent = HelperAgent(card, loop, registry)
    briefing = Briefing(
        kind="user_message",
        stream_id="u1",
        activation_id="act-test",
        platform="telegram",
        events=(),
        extras={"user_name": "Dain", "user_timezone": "UTC"},
    )
    run(agent.act(briefing))

    assert loop.calls[0]["acting_helper"] == "juniper"
    assert loop.calls[0]["turn_kind"] == "conversation"


def test_helper_agent_forwards_the_directors_execution_hints(db):
    """briefing.execution reaches the loop as `hints` - the §4.8 seam.
    vel's director hints nothing today (empty hints = the default
    route, byte-identical), but dynamic model routing is now a
    director-only change."""
    card = _card(id="juniper", telegram_handle="aria_bot")
    loop = _StubLoop()

    agent = HelperAgent(card, loop, ToolRegistry())
    briefing = Briefing(
        kind="user_message",
        stream_id="u1",
        activation_id="act-test",
        platform="telegram",
        events=(),
        extras={"user_name": "Dain", "user_timezone": "UTC"},
    )
    run(agent.act(briefing))

    assert loop.calls[0]["hints"] is briefing.execution
