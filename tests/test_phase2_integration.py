"""cross-seam integration for the v3 ensemble: the real ChatService driving the
real Orchestrator (with the real HelperStateManager + EventLog), only the
model-calling HelperAgent faked. this is the coverage no single workstream
could write - it proves onboarding routing, the director, scope tagging, and
delivery line up end to end.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database.database import init_db
from src.managers.event_log import EventLog
from src.managers.helper_state_manager import HelperStateManager, STATUS_ACTIVE
from src.managers.user_manager import UserManager
from src.models.unified_message import UnifiedMessage
from src.services.chat_service import ChatService
from src.services.orchestrator import Orchestrator


class FakeHelper:
    """a HelperAgent stand-in: records the briefing it was handed, replies with
    a canned line. name mirrors a real helper id."""
    def __init__(self, name):
        self.name = name
        self.briefings = []

    async def act(self, briefing):
        from src.agents.base import AgentOutcome
        self.briefings.append(briefing)
        return AgentOutcome(text=f"{self.name} says hi")


class RecordingDeliver:
    def __init__(self):
        self.sent = []  # (platform, target, text, speaker)

    async def __call__(self, platform, target_id, text, speaker="chordial"):
        self.sent.append((platform, target_id, text, speaker))
        return True


def _orch(agents, deliver=None):
    return Orchestrator(
        agents=agents,
        user_manager=UserManager(),
        deliver=deliver,
        helper_state_manager=HelperStateManager(),
    )


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _run(coro):
    # a fresh loop per call - other test modules close the shared loop, so
    # get_event_loop() would raise "no current event loop" mid-suite.
    return asyncio.run(coro)


def test_new_user_dm_routes_to_introduction():
    """a brand-new user's first dm becomes an introduction briefing for
    chordial (not a normal user_message)."""
    chordial = FakeHelper("chordial")
    chat = ChatService(orchestrator=_orch({"chordial": chordial}), user_manager=UserManager())

    msg = UnifiedMessage(content="hello?", platform_user_id="tg-new-1",
                         platform="telegram", platform_message_id="1",
                         chat_scope="dm", dm_helper="chordial")
    reply = _run(chat.process_message(msg))

    assert reply == "chordial says hi"
    assert chordial.briefings[-1].kind == "introduction"


def test_returning_active_user_dm_is_a_normal_turn():
    """once chordial is active for a user, their dm is a user_message."""
    um = UserManager()
    user_uuid, _ = _run(um.get_or_create_user("telegram", "tg-active-1"))
    _run(um.update_user_preferences(user_uuid, {"preferred_name": "dain"}))
    _run(HelperStateManager().set_status(user_uuid, "chordial", STATUS_ACTIVE))

    chordial = FakeHelper("chordial")
    chat = ChatService(orchestrator=_orch({"chordial": chordial}), user_manager=um)
    msg = UnifiedMessage(content="hey", platform_user_id="tg-active-1",
                         platform="telegram", platform_message_id="2",
                         chat_scope="dm", dm_helper="chordial")
    reply = _run(chat.process_message(msg))

    assert reply == "chordial says hi"
    assert chordial.briefings[-1].kind == "user_message"


def test_group_message_delivers_out_of_band_and_returns_none():
    """a group message is delivered per-speaker via the router; process_message
    returns None so the receiving bot echoes nothing."""
    um = UserManager()
    user_uuid, _ = _run(um.get_or_create_user("telegram", "tg-grp-1"))
    _run(um.update_user_preferences(user_uuid, {"preferred_name": "dain"}))
    _run(HelperStateManager().set_status(user_uuid, "chordial", STATUS_ACTIVE))

    chordial = FakeHelper("chordial")
    deliver = RecordingDeliver()
    chat = ChatService(orchestrator=_orch({"chordial": chordial}, deliver=deliver),
                       user_manager=um)
    msg = UnifiedMessage(content="hi crew", platform_user_id="tg-grp-1",
                         platform="telegram", platform_message_id="3",
                         chat_scope="group", group_chat_id="-100999", via_bot="chordial")
    reply = _run(chat.process_message(msg))

    assert reply is None                       # nothing sent by the receiving interface
    assert deliver.sent == [("telegram", "-100999", "chordial says hi", "chordial")]


def test_group_mention_routes_to_the_named_helper():
    """@-mentioning an active specialist in a group summons that helper, who
    speaks via its own bot."""
    um = UserManager()
    user_uuid, _ = _run(um.get_or_create_user("telegram", "tg-grp-2"))
    _run(um.update_user_preferences(user_uuid, {"preferred_name": "dain"}))
    hsm = HelperStateManager()
    _run(hsm.set_status(user_uuid, "chordial", STATUS_ACTIVE))
    _run(hsm.set_status(user_uuid, "tempo", STATUS_ACTIVE))

    agents = {"chordial": FakeHelper("chordial"), "tempo": FakeHelper("tempo")}
    deliver = RecordingDeliver()
    chat = ChatService(orchestrator=_orch(agents, deliver=deliver), user_manager=um)
    msg = UnifiedMessage(content="@tempo_bot got a workout?", platform_user_id="tg-grp-2",
                         platform="telegram", platform_message_id="4",
                         chat_scope="group", group_chat_id="-100999",
                         via_bot="chordial", mentioned=["tempo"])
    reply = _run(chat.process_message(msg))

    assert reply is None
    assert deliver.sent == [("telegram", "-100999", "tempo says hi", "tempo")]


def test_dm_transcript_stays_private_from_siblings():
    """a message in tempo's dm is scope-tagged so a sibling's briefing window
    never contains it (the privacy filter), while tempo's does."""
    um = UserManager()
    user_uuid, _ = _run(um.get_or_create_user("telegram", "tg-priv-1"))
    _run(um.update_user_preferences(user_uuid, {"preferred_name": "dain"}))
    hsm = HelperStateManager()
    _run(hsm.set_status(user_uuid, "chordial", STATUS_ACTIVE))
    _run(hsm.set_status(user_uuid, "tempo", STATUS_ACTIVE))

    tempo = FakeHelper("tempo")
    chat = ChatService(orchestrator=_orch({"chordial": FakeHelper("chordial"), "tempo": tempo},
                                          deliver=RecordingDeliver()), user_manager=um)
    # a private aside to tempo
    _run(chat.process_message(UnifiedMessage(
        content="secret between us", platform_user_id="tg-priv-1", platform="telegram",
        platform_message_id="5", chat_scope="dm", dm_helper="tempo")))

    log = EventLog(user_uuid)
    tempo_sees = [e.content for e in log.recent(visible_to="tempo") if e.kind == "message"]
    chordial_sees = [e.content for e in log.recent(visible_to="chordial") if e.kind == "message"]
    assert "secret between us" in tempo_sees
    assert "secret between us" not in chordial_sees
