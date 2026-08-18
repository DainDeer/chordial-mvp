"""cross-seam integration: the real ChatService driving the real Orchestrator
(with the real HelperStateManager + EventLog), only the model-calling
HelperAgent faked. this is the coverage no single workstream could write - it
proves onboarding routing, the director, scope tagging, and delivery line up
end to end. (7a: telegram is the single-bot tether - inbound is group scope
into the shared room, and the ensemble's @-mention summoning is gone; the
director owns who speaks.)
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
from src.services.orchestration import build_orchestrator


class FakeHelper:
    """a HelperAgent stand-in: records the briefing it was handed, replies with
    a canned line. name mirrors a real helper id."""
    def __init__(self, name):
        self.name = name
        self.briefings = []

    async def act(self, briefing):
        from src.agents import AgentOutcome
        self.briefings.append(briefing)
        return AgentOutcome(text=f"{self.name} says hi")


class RecordingDeliver:
    def __init__(self):
        self.sent = []  # (platform, target, text, speaker)

    async def __call__(self, platform, target_id, text, speaker="vel"):
        self.sent.append((platform, target_id, text, speaker))
        return True


def _orch(agents, deliver=None):
    return build_orchestrator(
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
    vel (not a normal user_message), delivered through the router."""
    vel = FakeHelper("vel")
    deliver = RecordingDeliver()
    chat = ChatService(orchestrator=_orch({"vel": vel}, deliver=deliver),
                       user_manager=UserManager())

    msg = UnifiedMessage(content="hello?", platform_user_id="tg-new-1",
                         platform="telegram", platform_message_id="1",
                         chat_scope="dm")
    reply = _run(chat.process_message(msg))

    assert reply is None            # router delivered; interface sends nothing
    assert deliver.sent == [("telegram", "tg-new-1", "vel says hi", "vel")]
    assert vel.briefings[-1].kind == "introduction"


def test_new_user_on_the_tether_routes_to_introduction():
    """7a: the same front door when the first contact is a tether message -
    group scope in, dm-shaped introduction out, delivered back to the
    private chat that sent it."""
    vel = FakeHelper("vel")
    deliver = RecordingDeliver()
    chat = ChatService(orchestrator=_orch({"vel": vel}, deliver=deliver),
                       user_manager=UserManager())

    msg = UnifiedMessage(content="hi there", platform_user_id="tg-new-2",
                         platform="telegram", platform_message_id="1",
                         chat_scope="group", group_chat_id="tg-new-2")
    reply = _run(chat.process_message(msg))

    assert reply is None
    assert deliver.sent == [("telegram", "tg-new-2", "vel says hi", "vel")]
    assert vel.briefings[-1].kind == "introduction"


def test_returning_active_user_dm_is_a_normal_turn():
    """once vel is active for a user, their dm is a user_message."""
    um = UserManager()
    user_uuid, _ = _run(um.get_or_create_user("telegram", "tg-active-1"))
    _run(um.update_user_preferences(user_uuid, {"preferred_name": "dain"}))
    _run(HelperStateManager().set_status(user_uuid, "vel", STATUS_ACTIVE))

    vel = FakeHelper("vel")
    deliver = RecordingDeliver()
    chat = ChatService(orchestrator=_orch({"vel": vel}, deliver=deliver),
                       user_manager=um)
    msg = UnifiedMessage(content="hey", platform_user_id="tg-active-1",
                         platform="telegram", platform_message_id="2",
                         chat_scope="dm")
    reply = _run(chat.process_message(msg))

    assert reply is None
    assert deliver.sent == [("telegram", "tg-active-1", "vel says hi", "vel")]
    assert vel.briefings[-1].kind == "user_message"


def test_group_message_delivers_out_of_band_and_returns_none():
    """a group message is delivered per-speaker via the router; process_message
    returns None so the receiving interface echoes nothing. on the tether the
    group target is the sender's own private chat."""
    um = UserManager()
    user_uuid, _ = _run(um.get_or_create_user("telegram", "tg-grp-1"))
    _run(um.update_user_preferences(user_uuid, {"preferred_name": "dain"}))
    _run(HelperStateManager().set_status(user_uuid, "vel", STATUS_ACTIVE))

    vel = FakeHelper("vel")
    deliver = RecordingDeliver()
    chat = ChatService(orchestrator=_orch({"vel": vel}, deliver=deliver),
                       user_manager=um)
    msg = UnifiedMessage(content="hi crew", platform_user_id="tg-grp-1",
                         platform="telegram", platform_message_id="3",
                         chat_scope="group", group_chat_id="tg-grp-1")
    reply = _run(chat.process_message(msg))

    assert reply is None                       # nothing sent by the receiving interface
    assert deliver.sent == [("telegram", "tg-grp-1", "vel says hi", "vel")]


def test_introduction_talk_stays_private_from_siblings():
    """an introduction is dm-shaped wherever it runs: identity talk during
    vel's front door is scope-tagged so a sibling's briefing window never
    contains it, while vel's does."""
    um = UserManager()
    chat = ChatService(
        orchestrator=_orch({"vel": FakeHelper("vel"),
                            "skip": FakeHelper("skip")},
                           deliver=RecordingDeliver()),
        user_manager=um)
    # a brand-new person's first words - the introduction, private with vel
    _run(chat.process_message(UnifiedMessage(
        content="secret between us", platform_user_id="tg-priv-1",
        platform="telegram", platform_message_id="5",
        chat_scope="group", group_chat_id="tg-priv-1")))

    user_uuid = _run(um.lookup_user_uuid("telegram", "tg-priv-1"))
    assert user_uuid is not None
    log = EventLog(user_uuid)
    vel_sees = [e.content for e in log.recent(visible_to="vel")
                if e.kind == "message"]
    skip_sees = [e.content for e in log.recent(visible_to="skip")
                 if e.kind == "message"]
    assert "secret between us" in vel_sees
    assert "secret between us" not in skip_sees
