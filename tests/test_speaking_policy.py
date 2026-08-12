"""the speaking spine (phase 3b, ROOMS_DESIGN.md §3): routing rules first,
decider only in the genuine gray zone, chair on every failure path.

layer contract under test:
- the decider is one enum-constrained utility call whose reply must be
  exactly a candidate id - prose, invented helpers, and provider errors all
  collapse to None, never a guess
- the director consults it ONLY for a shared-room message with no mention
  and >1 candidate; a lone candidate, a missing decider, or a decider
  failure is the chair, for free
- candidates = the user's active cast, built agents only, filtered by the
  card's default_rooms for this room type; the chair always leads
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dainframe.core import Stimulus  # noqa: E402

from src.services.orchestration import ChordialDirector  # noqa: E402
from src.services.speaking_policy import SpeakingDecider  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- the decider service -----------------------------------------------------


class FakeProvider:
    model = "fake-utility"

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.requests = []

    async def create_message(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.reply, usage=None)


def _decide(provider, **kwargs):
    decider = SpeakingDecider(provider, "anthropic")
    defaults = dict(
        message="what should i eat before my run?",
        candidate_ids=["vel", "skip", "remy"],
        chair_id="vel",
    )
    defaults.update(kwargs)
    return run(decider.decide(**defaults))


def test_decider_returns_a_valid_pick():
    assert _decide(FakeProvider("remy")) == "remy"


def test_decider_tolerates_whitespace_and_punctuation():
    assert _decide(FakeProvider("  Remy.\n")) == "remy"


def test_decider_rejects_prose_and_invented_helpers():
    # a reply that isn't exactly a candidate id is None, never a guess
    assert _decide(FakeProvider("i think remy should take this one")) is None
    assert _decide(FakeProvider("aria")) is None
    assert _decide(FakeProvider("")) is None
    assert _decide(FakeProvider(None)) is None


def test_decider_never_raises():
    assert _decide(FakeProvider(error=RuntimeError("provider down"))) is None


def test_decider_prompt_carries_candidates_and_recent_context():
    provider = FakeProvider("skip")
    recent = [
        SimpleNamespace(kind="message", author_type="user", author="user",
                        content="thinking about a jog later"),
        SimpleNamespace(kind="message", author_type="agent", author="skip",
                        content="ooh yes! evening jog club!"),
        SimpleNamespace(kind="action", author_type="agent", author="skip",
                        content="[tool stuff]"),  # actions never rendered
    ]
    assert _decide(provider, recent=recent) == "skip"
    prompt = provider.requests[0].messages[0].content
    assert "- vel (the chair):" in prompt
    assert "- skip:" in prompt and "movement" in prompt
    assert "[user] thinking about a jog later" in prompt
    assert "[skip] ooh yes! evening jog club!" in prompt
    assert "[tool stuff]" not in prompt


def test_decider_records_usage_per_user():
    calls = []

    class Recorder:
        def record_call(self, **kwargs):
            calls.append(kwargs)

    decider = SpeakingDecider(FakeProvider("remy"), "anthropic",
                              usage_recorder=Recorder())
    run(decider.decide(message="lunch?", candidate_ids=["vel", "remy"],
                       chair_id="vel", user_uuid="u1", platform="app"))
    assert calls and calls[0]["user_uuid"] == "u1"
    assert calls[0]["role"] == "decider"


# --- the director's gray zone ------------------------------------------------


class FakeView:
    def __init__(self, helper_id):
        self.helper_id = helper_id
        self.is_active = True


class FakeHSM:
    def __init__(self, active):
        self._ids = list(active)

    async def active_helpers(self, user_uuid):
        return [FakeView(h) for h in self._ids]


class StubDecider:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


async def _daily(stream_id):
    return "daily"


def _director(agent_ids, active, decider=None, room_type="daily"):
    async def room_type_of(stream_id):
        return room_type
    return ChordialDirector(
        agent_ids,
        helper_state_manager=FakeHSM(active),
        decider=decider,
        room_type_of=room_type_of,
    )


def _room_message(content="what should i eat before my run?"):
    return Stimulus(kind="user_message", stream_id="room-1", scope="group",
                    content=content)


def _speaker(script):
    assert len(script.lines) == 1
    return script.lines[0].speaker


def test_gray_zone_consults_the_decider_and_casts_its_pick():
    decider = StubDecider("remy")
    d = _director(["vel", "skip", "remy"], active=("vel", "skip", "remy"),
                  decider=decider)
    script = run(d.direct(_room_message(), None))
    assert _speaker(script) == "remy"
    assert decider.calls[0]["candidate_ids"][0] == "vel"  # chair leads


def test_single_candidate_cast_never_calls_the_decider():
    """a fresh user's cast is just the chair - routing must cost zero
    tokens."""
    decider = StubDecider("skip")
    d = _director(["vel", "skip"], active=("vel",), decider=decider)
    script = run(d.direct(_room_message(), None))
    assert _speaker(script) == "vel"
    assert decider.calls == []


def test_mentions_bypass_the_decider():
    decider = StubDecider("remy")
    d = _director(["vel", "skip", "remy"], active=("vel", "skip", "remy"),
                  decider=decider)
    s = Stimulus(kind="user_message", stream_id="room-1", scope="group",
                 content="@skip you around?", addressed=("skip",))
    script = run(d.direct(s, None))
    assert _speaker(script) == "skip"
    assert decider.calls == []


def test_no_decider_wired_means_the_chair_fields_it():
    d = _director(["vel", "skip", "remy"], active=("vel", "skip", "remy"))
    assert _speaker(run(d.direct(_room_message(), None))) == "vel"


def test_decider_failure_falls_back_to_the_chair():
    d = _director(["vel", "skip", "remy"], active=("vel", "skip", "remy"),
                  decider=StubDecider(None))
    assert _speaker(run(d.direct(_room_message(), None))) == "vel"


def test_decider_cannot_cast_outside_the_candidates():
    """belt and braces: even a buggy decider returning a non-candidate (an
    inactive helper, an invented one) must not reach the cast."""
    d = _director(["vel", "skip", "remy"], active=("vel", "skip", "remy"),
                  decider=StubDecider("mabel"))
    assert _speaker(run(d.direct(_room_message(), None))) == "vel"


def test_candidates_respect_room_type_presence():
    """skip's card lives in daily rooms only; in a cycle room skip is not a
    candidate, so a two-lane cast collapses to the one eligible specialist."""
    decider = StubDecider("edwin")
    d = _director(["vel", "skip", "edwin"], active=("vel", "skip", "edwin"),
                  decider=decider, room_type="cycle")
    script = run(d.direct(_room_message("how did this cycle go?"), None))
    assert _speaker(script) == "edwin"
    assert decider.calls[0]["candidate_ids"] == ["vel", "edwin"]


def test_legacy_rooms_count_as_daily():
    decider = StubDecider("skip")
    d = _director(["vel", "skip"], active=("vel", "skip"),
                  decider=decider, room_type="legacy")
    script = run(d.direct(_room_message("feeling like a walk"), None))
    assert _speaker(script) == "skip"


# --- sol's review: the production shapes -------------------------------------


class FakeReader:
    """the engine's ReadOnlyEventReader shape: read/latest, NOT iterable -
    the exact production object that silently disabled the decider once."""

    def __init__(self, events=(), error=None):
        self.events = list(events)
        self.error = error
        self.queries = []

    async def read(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.events)


def _msg(author_type, author, content, scope=None):
    return SimpleNamespace(kind="message", author_type=author_type,
                           author=author, content=content, scope=scope)


def _action(author, content="[did tool things]"):
    return SimpleNamespace(kind="action", author_type="agent",
                           author=author, content=content, scope=None)


def test_decider_runs_against_the_engines_reader_shape():
    """the P1: direct() receives an EventReader, not a list. the director
    must materialize the window itself - if the reader shape ever reaches
    list() again, this test fails the way production silently did."""
    decider = StubDecider("remy")
    d = _director(["vel", "skip", "remy"], active=("vel", "skip", "remy"),
                  decider=decider)
    reader = FakeReader([_msg("user", "user", "thinking about dinner")])
    script = run(d.direct(_room_message("what should i cook?"), reader))
    assert _speaker(script) == "remy"
    assert reader.queries, "the director must read the recent window"
    assert decider.calls[0]["recent"][0].content == "thinking about dinner"


def test_reader_failure_still_decides_without_context():
    decider = StubDecider("remy")
    d = _director(["vel", "skip", "remy"], active=("vel", "skip", "remy"),
                  decider=decider)
    reader = FakeReader(error=RuntimeError("db hiccup"))
    script = run(d.direct(_room_message("what should i cook?"), reader))
    assert _speaker(script) == "remy"          # decided, not defaulted
    assert decider.calls[0]["recent"] == []    # just with less context


def test_tail_filters_before_slicing_and_drops_the_inbound_echo():
    """the P2: a tool-heavy previous turn must not evict the user message a
    follow-up needs, dm lines never reach the routing prompt, and the
    just-recorded inbound isn't repeated as context."""
    provider = FakeProvider("skip")
    inbound = "okay do that again"
    recent = [
        _msg("user", "user", "can someone plan me a tiny workout?"),
        _action("skip"), _action("skip"), _action("skip"), _action("skip"),
        _msg("agent", "skip", "one-song warmup, then a lap!"),
        _msg("agent", "mabel", "a secret dm aside", scope="dm"),
        _msg("user", "user", inbound),   # the engine already recorded it
    ]
    assert _decide(provider, message=inbound, recent=recent) == "skip"
    prompt = provider.requests[0].messages[0].content
    assert "[user] can someone plan me a tiny workout?" in prompt
    assert "[skip] one-song warmup, then a lap!" in prompt
    assert "secret dm aside" not in prompt
    assert prompt.count(inbound) == 1          # the message itself, once


def test_candidates_respect_platform_deliverability():
    """the P2: a single-bot telegram deployment can only send as the chair -
    a specialist without an interface must never be cast (the router would
    fail closed and the user would get silence)."""
    decider = StubDecider("skip")

    def only_vel(platform):
        assert platform == "telegram"
        return {"vel"}

    d = ChordialDirector(
        ["vel", "skip", "remy"],
        helper_state_manager=FakeHSM(("vel", "skip", "remy")),
        decider=decider,
        room_type_of=_daily,
        deliverable_speakers=only_vel,
    )
    s = Stimulus(kind="user_message", stream_id="room-1", scope="group",
                 content="feeling like a walk", platform="telegram")
    script = run(d.direct(s, None))
    assert _speaker(script) == "vel"
    assert decider.calls == []   # one deliverable candidate = zero tokens


def test_unrestricted_platforms_keep_the_full_candidate_list():
    decider = StubDecider("skip")
    d = ChordialDirector(
        ["vel", "skip"],
        helper_state_manager=FakeHSM(("vel", "skip")),
        decider=decider,
        room_type_of=_daily,
        deliverable_speakers=lambda platform: None,   # app-style: any speaker
    )
    s = Stimulus(kind="user_message", stream_id="room-1", scope="group",
                 content="feeling like a walk", platform="app")
    assert _speaker(run(d.direct(s, None))) == "skip"


def test_router_deliverable_speakers_shapes():
    from src.services.message_router import MessageRouter

    class Iface:
        def __init__(self, platform, helper_id=None):
            self.platform = platform
            if helper_id is not None:
                self.helper_id = helper_id

    router = MessageRouter(user_manager=None)
    router.register(Iface("telegram", "vel"))          # single-bot deployment
    router.register(Iface("app"))                      # in-band attribution
    assert router.deliverable_speakers("telegram") == {"vel"}
    assert router.deliverable_speakers("app") is None  # unrestricted
    assert router.deliverable_speakers("discord") == set()  # nothing there
