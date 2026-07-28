"""director tests: the rules-only casting of a Script.

ChordialDirector (the dainframe Director seam) replaces the old
Orchestrator._direct. it decides WHO speaks for a stimulus - deterministically
this phase (the next phase makes the group no-mention branch an ai call) - and
with what per-line obligations. these lock the routing rules: dm -> the
addressed helper; group + @mention -> the mentioned active helpers (in order,
deduped, capped at 2); group no-mention -> chordial; introduction -> the
addressed helper; and the hard guardrail that a conversational path never
returns an empty/broken script.

the director touches no db (it only reads a faked HelperStateManager), so
these are plain fakes - no temp database needed.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dainframe.core import DeliveryTarget, Stimulus  # noqa: E402

from src.services.orchestration import ChordialDirector  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeView:
    def __init__(self, helper_id, is_active=True):
        self.helper_id = helper_id
        self.is_active = is_active


class FakeHSM:
    """returns a fixed active cast; chordial is always present, like the real
    manager (a user always has the generalist)."""
    def __init__(self, active=("chordial",)):
        ids = list(active)
        if "chordial" not in ids:
            ids.insert(0, "chordial")
        self._ids = ids

    async def active_helpers(self, user_uuid):
        return [FakeView(h, True) for h in self._ids]


def _director(agent_ids, active=("chordial",)):
    return ChordialDirector(agent_ids, helper_state_manager=FakeHSM(active))


def _speakers(script):
    return [line.speaker for line in script.lines]


def _direct(director, stimulus):
    return run(director.direct(stimulus, None))  # rules read no events


# --- dm routing --------------------------------------------------------------


def test_dm_routes_to_the_addressed_helper():
    d = _director(["chordial", "tempo"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="dm",
                 audience="tempo", addressed=("tempo",))
    assert _speakers(_direct(d, s)) == ["tempo"]


def test_dm_without_addressee_falls_back_to_chordial():
    d = _director(["chordial", "tempo"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="dm")
    assert _speakers(_direct(d, s)) == ["chordial"]


def test_dm_default_scope_is_dm():
    """a bare user_message (the v2 shape) is a dm to chordial."""
    d = _director(["chordial"])
    s = Stimulus(kind="user_message", stream_id="u1", content="hi")
    script = _direct(d, s)
    assert _speakers(script) == ["chordial"]
    assert script.lines[0].event_context.scope == "dm"
    assert script.lines[0].event_context.audience == "chordial"


def test_dm_line_is_required_and_direct():
    d = _director(["chordial", "tempo"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="dm",
                 addressed=("tempo",))
    line = _direct(d, s).lines[0]
    assert (line.response, line.delivery) == ("required", "direct")
    # the resolved speaker names the private channel
    assert line.event_context.audience == "tempo"


# --- group routing -----------------------------------------------------------


def test_group_no_mention_goes_to_chordial():
    d = _director(["chordial", "tempo"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="group")
    script = _direct(d, s)
    assert _speakers(script) == ["chordial"]
    assert script.lines[0].event_context.scope == "group"
    assert script.lines[0].event_context.audience is None


def test_group_mentions_route_in_order():
    d = _director(["chordial", "tempo", "aria"], active=("tempo", "aria"))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("aria", "tempo"))
    assert _speakers(_direct(d, s)) == ["aria", "tempo"]


def test_group_mentions_are_capped_at_two():
    d = _director(["chordial", "tempo", "aria", "pep"],
                  active=("tempo", "aria", "pep"))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("tempo", "aria", "pep"))
    assert _speakers(_direct(d, s)) == ["tempo", "aria"]


def test_group_mentions_are_deduped():
    d = _director(["chordial", "tempo"], active=("tempo",))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("tempo", "tempo", "tempo"))
    assert _speakers(_direct(d, s)) == ["tempo"]


def test_inactive_mention_is_dropped():
    d = _director(["chordial", "tempo", "aria"], active=("tempo",))  # aria inactive
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("aria", "tempo"))
    assert _speakers(_direct(d, s)) == ["tempo"]


def test_unknown_mention_is_dropped():
    d = _director(["chordial", "tempo"], active=("tempo", "ghost"))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("ghost", "tempo"))
    assert _speakers(_direct(d, s)) == ["tempo"]


def test_group_all_mentions_unroutable_falls_back_to_chordial():
    d = _director(["chordial", "tempo"])  # nobody else active
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("ghost", "aria"))
    assert _speakers(_direct(d, s)) == ["chordial"]


# --- the other stimulus kinds ------------------------------------------------


def test_curation_routes_to_a_silent_curator_line():
    d = _director(["chordial", "curator"])
    s = Stimulus(kind="curation_due", stream_id="u1", record_inbound=False)
    line = _direct(d, s).lines[0]
    assert line.speaker == "curator"
    assert (line.response, line.delivery) == ("silent", "none")


def test_scheduled_tick_is_an_optional_pending_chordial_line():
    """generation and delivery are separate scheduler phases (until the
    pulse): the line is PENDING, and OPTIONAL - a quiet tick is a non-event,
    never an error."""
    d = _director(["chordial"])
    target = DeliveryTarget(platform="discord", target_id="42")
    s = Stimulus(kind="scheduled_tick", stream_id="u1", platform="discord",
                 record_inbound=False, target=target)
    line = _direct(d, s).lines[0]
    assert line.speaker == "chordial"
    assert (line.response, line.delivery) == ("optional", "pending")
    assert line.target == target
    assert line.event_context.outbound_message_type == "scheduled"
    assert line.event_context.audience == "chordial"


def test_introduction_routes_to_the_addressed_helper():
    d = _director(["chordial", "aria"])
    s = Stimulus(kind="introduction", stream_id="u1", addressed=("aria",))
    assert _speakers(_direct(d, s)) == ["aria"]


def test_introduction_without_helper_falls_back_to_chordial():
    d = _director(["chordial", "aria"])
    s = Stimulus(kind="introduction", stream_id="u1")
    assert _speakers(_direct(d, s)) == ["chordial"]


def test_introduction_unknown_helper_falls_back_to_chordial():
    d = _director(["chordial"])  # aria has no agent
    s = Stimulus(kind="introduction", stream_id="u1", addressed=("aria",))
    assert _speakers(_direct(d, s)) == ["chordial"]


def test_unknown_kind_is_an_explicit_noop():
    d = _director(["chordial"])
    s = Stimulus(kind="mystery", stream_id="u1")
    script = _direct(d, s)
    assert script.lines == ()
    assert "mystery" in script.noop_reason
