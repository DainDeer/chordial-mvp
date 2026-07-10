"""director tests: the rules-only casting of a Script (phase 2).

`Orchestrator._direct` replaces v2's static `_select`. it decides WHO speaks
for a stimulus, deterministically this phase (phase 3 makes the group no-mention
branch an ai call). these lock the routing rules: dm -> the dm'd helper; group
+ @mention -> the mentioned active helpers (in order, deduped, capped at 2);
group no-mention -> chordial; introduction -> the intro helper; and the hard
guardrail that the director never returns an empty/broken script.

_direct touches no db (it only reads a faked HelperStateManager), so these are
plain fakes - no temp database needed.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.base import AgentOutcome  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.orchestrator import Orchestrator, Stimulus  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeAgent:
    def __init__(self, name):
        self.name = name

    async def act(self, briefing):
        return AgentOutcome(text=f"{self.name} speaks")


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


def _orch(agent_ids, active=("chordial",)):
    agents = {aid: FakeAgent(aid) for aid in agent_ids}
    return Orchestrator(agents=agents, user_manager=UserManager(),
                        helper_state_manager=FakeHSM(active))


def _speakers(script):
    return [line.speaker for line in script.lines]


def _direct(orch, stimulus):
    return run(orch._direct(stimulus, log=None))  # _direct doesn't read the log


# --- dm routing --------------------------------------------------------------

def test_dm_routes_to_the_dm_helper():
    orch = _orch(["chordial", "tempo"])
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="dm", dm_helper="tempo")
    assert _speakers(_direct(orch, s)) == ["tempo"]


def test_dm_without_helper_falls_back_to_chordial():
    orch = _orch(["chordial"])
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="dm", dm_helper=None)
    assert _speakers(_direct(orch, s)) == ["chordial"]


def test_dm_default_scope_is_dm():
    """a stimulus with no chat_scope set is a dm (the legacy single-helper path)."""
    orch = _orch(["chordial"])
    s = Stimulus(kind="user_message", user_uuid="u1", content="hi")
    assert _speakers(_direct(orch, s)) == ["chordial"]


# --- group routing -----------------------------------------------------------

def test_group_no_mention_goes_to_chordial():
    orch = _orch(["chordial", "tempo"], active=("chordial", "tempo"))
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="group", mentioned=[])
    assert _speakers(_direct(orch, s)) == ["chordial"]


def test_group_mentions_route_in_order():
    orch = _orch(["chordial", "tempo", "aria"], active=("chordial", "tempo", "aria"))
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="group",
                 mentioned=["aria", "tempo"])
    assert _speakers(_direct(orch, s)) == ["aria", "tempo"]


def test_group_mentions_are_capped_at_two():
    orch = _orch(["chordial", "tempo", "aria", "mochi"],
                 active=("chordial", "tempo", "aria", "mochi"))
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="group",
                 mentioned=["tempo", "aria", "mochi"])
    assert _speakers(_direct(orch, s)) == ["tempo", "aria"]


def test_group_mentions_are_deduped():
    orch = _orch(["chordial", "tempo"], active=("chordial", "tempo"))
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="group",
                 mentioned=["tempo", "tempo"])
    assert _speakers(_direct(orch, s)) == ["tempo"]


def test_inactive_mention_is_dropped():
    # tempo is mentioned but not in the active cast -> filtered out
    orch = _orch(["chordial", "tempo"], active=("chordial",))
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="group",
                 mentioned=["tempo"])
    assert _speakers(_direct(orch, s)) == ["chordial"]  # all-inactive -> fallback


def test_unknown_mention_is_dropped():
    # "ghost" is active per state but has no agent wired -> filtered out
    orch = _orch(["chordial", "tempo"], active=("chordial", "tempo", "ghost"))
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="group",
                 mentioned=["ghost", "tempo"])
    assert _speakers(_direct(orch, s)) == ["tempo"]


def test_group_all_mentions_unroutable_falls_back_to_chordial():
    orch = _orch(["chordial", "tempo"], active=("chordial",))
    s = Stimulus(kind="user_message", user_uuid="u1", chat_scope="group",
                 mentioned=["ghost", "phantom"])
    assert _speakers(_direct(orch, s)) == ["chordial"]


# --- other kinds -------------------------------------------------------------

def test_curation_routes_to_curator():
    orch = _orch(["chordial", "curator"])
    s = Stimulus(kind="curation_due", user_uuid="u1")
    assert _speakers(_direct(orch, s)) == ["curator"]


def test_scheduled_tick_routes_to_chordial():
    orch = _orch(["chordial"])
    s = Stimulus(kind="scheduled_tick", user_uuid="u1")
    assert _speakers(_direct(orch, s)) == ["chordial"]


def test_introduction_routes_to_intro_helper():
    orch = _orch(["chordial", "aria"])
    s = Stimulus(kind="introduction", user_uuid="u1", intro_helper="aria")
    assert _speakers(_direct(orch, s)) == ["aria"]


def test_introduction_without_helper_falls_back_to_chordial():
    orch = _orch(["chordial"])
    s = Stimulus(kind="introduction", user_uuid="u1", intro_helper=None)
    assert _speakers(_direct(orch, s)) == ["chordial"]


def test_introduction_unknown_helper_falls_back_to_chordial():
    orch = _orch(["chordial"])  # no agent for "aria"
    s = Stimulus(kind="introduction", user_uuid="u1", intro_helper="aria")
    assert _speakers(_direct(orch, s)) == ["chordial"]


def test_unknown_kind_casts_nobody():
    orch = _orch(["chordial"])
    s = Stimulus(kind="mystery", user_uuid="u1")
    assert _speakers(_direct(orch, s)) == []
