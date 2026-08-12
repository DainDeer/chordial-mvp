"""the app room is a council room (phase 3b).

before this phase every app message rode the dm path (the chair always
answered). now the desktop room is a SHARED scope: the routing spine picks
the speaker. two invariants matter:

- an ordinary app message becomes a group-scope stimulus in today's room,
  with the user riding extras (the identity split), so the director's
  candidate/decider pass runs
- a brand-new person's first app message still lands as the chair's
  INTRODUCTION, dm-shaped (identity talk stays out of shared summaries) -
  the app is many users' first surface and must not skip the front door
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base
from src.models.unified_message import UnifiedMessage
from src.services.chat_service import ChatService
from src.managers.helper_state_manager import HelperStateManager
from src.managers.user_manager import UserManager


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


class CapturingOrchestrator:
    def __init__(self):
        self.stimuli = []

    async def handle(self, stimulus):
        self.stimuli.append(stimulus)
        from types import SimpleNamespace
        return SimpleNamespace(any_delivered=True, refused=False,
                               error=None, reply=None, replies=())


# the app platform id the server posts with. in production this IS the
# user_uuid (the device token proved who they are); tests keep the two
# strings distinct to catch any conflation of platform id and identity.
PUID = "app-device-user"


def _app_message(content="hello room"):
    """the exact shape the web server posts (phase 3b): the app room is the
    council's shared room, group id = the user."""
    return UnifiedMessage(
        content=content,
        platform_user_id=PUID,
        platform="app",
        platform_message_id="m1",
        chat_scope="group",
        group_chat_id=PUID,
    )


async def _new_user():
    user_uuid, _ = await UserManager().get_or_create_user("app", PUID)
    return user_uuid


async def _known_user(name="megan"):
    user_uuid = await _new_user()
    # an established user: named, with the chair already met
    await UserManager().update_user_preferences(user_uuid, {"preferred_name": name})
    await HelperStateManager().set_status(user_uuid, "vel", "active")
    return user_uuid


def test_app_message_becomes_a_group_stimulus_in_todays_room(env):
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    user_uuid = run(_known_user())

    run(svc.process_message(_app_message()))

    stim = orch.stimuli[0]
    assert stim.kind == "user_message"
    assert stim.scope == "group"                      # the council's room
    assert stim.stream_id != user_uuid                # a real daily room
    assert stim.extras["user_id"] == user_uuid        # identity split
    assert stim.target.platform == "app"
    assert stim.target.target_id == PUID              # fan-out key


def test_new_app_user_still_gets_the_chairs_introduction(env):
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    user_uuid = run(_new_user())

    run(svc.process_message(_app_message("hi! what is this?")))

    stim = orch.stimuli[0]
    assert stim.kind == "introduction"
    assert stim.addressed == ("vel",)
    assert stim.scope == "dm"          # identity talk stays out of summaries
    assert stim.audience == "vel"
    state = run(HelperStateManager().get(user_uuid, "vel"))
    assert state.status == "introducing"


def test_introduced_app_user_gets_ordinary_group_turns_thereafter(env):
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    user_uuid = run(_known_user())

    run(svc.process_message(_app_message("morning!")))
    run(svc.process_message(_app_message("what's on today?")))

    assert [s.kind for s in orch.stimuli] == ["user_message", "user_message"]
    assert all(s.scope == "group" for s in orch.stimuli)


def test_telegram_dms_keep_the_dm_shape(env):
    """the app change must not leak: a telegram dm stays a private 1:1."""
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    user_uuid, _ = run(UserManager().get_or_create_user("telegram", "42"))
    run(UserManager().update_user_preferences(user_uuid, {"preferred_name": "megan"}))
    run(HelperStateManager().set_status(user_uuid, "vel", "active"))

    run(svc.process_message(UnifiedMessage(
        content="hey", platform_user_id="42", platform="telegram",
        platform_message_id="m2")))

    stim = orch.stimuli[0]
    assert stim.scope == "dm"
    assert stim.addressed == ("vel",)
