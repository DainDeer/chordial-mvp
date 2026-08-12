"""director tests: the rules-only casting of a Script.

ChordialDirector (the dainframe Director seam) replaces the old
Orchestrator._direct. it decides WHO speaks for a stimulus - deterministically
this phase (the next phase makes the group no-mention branch an ai call) - and
with what per-line obligations. these lock the routing rules: dm -> the
addressed helper; group + @mention -> the mentioned active helpers (in order,
deduped, capped at 2); group no-mention -> vel; introduction -> the
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
    """returns a fixed active cast; vel is always present, like the real
    manager (a user always has the generalist)."""
    def __init__(self, active=("vel",)):
        ids = list(active)
        if "vel" not in ids:
            ids.insert(0, "vel")
        self._ids = ids

    async def active_helpers(self, user_uuid):
        return [FakeView(h, True) for h in self._ids]


def _director(agent_ids, active=("vel",)):
    return ChordialDirector(agent_ids, helper_state_manager=FakeHSM(active))


def _speakers(script):
    return [line.speaker for line in script.lines]


def _direct(director, stimulus):
    return run(director.direct(stimulus, None))  # rules read no events


# --- dm routing --------------------------------------------------------------


def test_dm_routes_to_the_addressed_helper():
    d = _director(["vel", "skip"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="dm",
                 audience="skip", addressed=("skip",))
    assert _speakers(_direct(d, s)) == ["skip"]


def test_dm_without_addressee_falls_back_to_chordial():
    d = _director(["vel", "skip"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="dm")
    assert _speakers(_direct(d, s)) == ["vel"]


def test_dm_default_scope_is_dm():
    """a bare user_message (the v2 shape) is a dm to vel."""
    d = _director(["vel"])
    s = Stimulus(kind="user_message", stream_id="u1", content="hi")
    script = _direct(d, s)
    assert _speakers(script) == ["vel"]
    assert script.lines[0].event_context.scope == "dm"
    assert script.lines[0].event_context.audience == "vel"


def test_dm_line_is_required_and_direct():
    d = _director(["vel", "skip"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="dm",
                 addressed=("skip",))
    line = _direct(d, s).lines[0]
    assert (line.response, line.delivery) == ("required", "direct")
    # the resolved speaker names the private channel
    assert line.event_context.audience == "skip"


# --- group routing -----------------------------------------------------------


def test_group_no_mention_goes_to_chordial():
    d = _director(["vel", "skip"])
    s = Stimulus(kind="user_message", stream_id="u1", scope="group")
    script = _direct(d, s)
    assert _speakers(script) == ["vel"]
    assert script.lines[0].event_context.scope == "group"
    assert script.lines[0].event_context.audience is None


def test_group_mentions_route_in_order():
    d = _director(["vel", "skip", "juniper"], active=("skip", "juniper"))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("juniper", "skip"))
    assert _speakers(_direct(d, s)) == ["juniper", "skip"]


def test_group_mentions_are_capped_at_two():
    d = _director(["vel", "skip", "juniper", "pip"],
                  active=("skip", "juniper", "pip"))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("skip", "juniper", "pip"))
    assert _speakers(_direct(d, s)) == ["skip", "juniper"]


def test_group_mentions_are_deduped():
    d = _director(["vel", "skip"], active=("skip",))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("skip", "skip", "skip"))
    assert _speakers(_direct(d, s)) == ["skip"]


def test_inactive_mention_is_dropped():
    d = _director(["vel", "skip", "juniper"], active=("skip",))  # juniper inactive
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("juniper", "skip"))
    assert _speakers(_direct(d, s)) == ["skip"]


def test_unknown_mention_is_dropped():
    d = _director(["vel", "skip"], active=("skip", "ghost"))
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("ghost", "skip"))
    assert _speakers(_direct(d, s)) == ["skip"]


def test_group_all_mentions_unroutable_falls_back_to_chordial():
    d = _director(["vel", "skip"])  # nobody else active
    s = Stimulus(kind="user_message", stream_id="u1", scope="group",
                 addressed=("ghost", "juniper"))
    assert _speakers(_direct(d, s)) == ["vel"]


# --- the other stimulus kinds ------------------------------------------------


def test_curation_routes_to_a_silent_curator_line():
    d = _director(["vel", "curator"])
    s = Stimulus(kind="curation_due", stream_id="u1", record_inbound=False)
    line = _direct(d, s).lines[0]
    assert line.speaker == "curator"
    assert (line.response, line.delivery) == ("silent", "none")


def test_scheduled_tick_is_an_optional_direct_chordial_line():
    """ambient outreach rides the ordinary direct path (the pulse owns
    WHEN; the engine owns generation->delivery->recording as one serialized
    activation). the line stays OPTIONAL - a quiet tick is a non-event,
    never an error."""
    d = _director(["vel"])
    target = DeliveryTarget(platform="discord", target_id="42")
    s = Stimulus(kind="scheduled_tick", stream_id="u1", platform="discord",
                 record_inbound=False, target=target)
    line = _direct(d, s).lines[0]
    assert line.speaker == "vel"
    assert (line.response, line.delivery) == ("optional", "direct")
    assert line.target == target
    assert line.event_context.outbound_message_type == "scheduled"
    assert line.event_context.audience == "vel"


def test_introduction_routes_to_the_addressed_helper():
    d = _director(["vel", "juniper"])
    s = Stimulus(kind="introduction", stream_id="u1", addressed=("juniper",))
    assert _speakers(_direct(d, s)) == ["juniper"]


def test_introduction_without_helper_falls_back_to_chordial():
    d = _director(["vel", "juniper"])
    s = Stimulus(kind="introduction", stream_id="u1")
    assert _speakers(_direct(d, s)) == ["vel"]


def test_introduction_unknown_helper_falls_back_to_chordial():
    d = _director(["vel"])  # juniper has no agent
    s = Stimulus(kind="introduction", stream_id="u1", addressed=("juniper",))
    assert _speakers(_direct(d, s)) == ["vel"]


def test_unknown_kind_is_an_explicit_noop():
    d = _director(["vel"])
    s = Stimulus(kind="mystery", stream_id="u1")
    script = _direct(d, s)
    assert script.lines == ()
    assert "mystery" in script.noop_reason
