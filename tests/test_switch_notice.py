"""platform-switch notice tests: the one-time courtesy sent to the platform a
conversation just walked away from.

the semantics under test (owner's spec): when a user message arrives on a
different platform than their previous message, send ONE notice to the old
platform - and don't repeat it until the user next messages there. the trigger
is structurally self-deduping (it compares against the last USER message's
platform), with a note event recorded as the restart-safe audit trail.
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
from src.database.models import Base, User, PlatformIdentity, ConversationEvent  # noqa: E402
from src.agents.base import AgentOutcome  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.orchestrator import Orchestrator, Stimulus  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain", timezone="UTC"))
        s.add(PlatformIdentity(user_uuid="u1", platform="discord",
                               platform_user_id="d-1", is_active=True))
        s.add(PlatformIdentity(user_uuid="u1", platform="telegram",
                               platform_user_id="t-1", is_active=True))
        s.commit()
    yield TestSession
    engine.dispose()


class FakeCompanion:
    name = "chordial"

    async def act(self, briefing):
        return AgentOutcome(text="hi there!")


class FakeDeliver:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []  # (platform, platform_user_id, message)

    async def __call__(self, platform, platform_user_id, message):
        self.sent.append((platform, platform_user_id, message))
        return self.ok


def _orch(deliver):
    return Orchestrator(
        agents={"chordial": FakeCompanion()},
        user_manager=UserManager(),
        deliver=deliver,
    )


def _say(orch, text, platform):
    return run(orch.handle(Stimulus(
        kind="user_message", user_uuid="u1", platform=platform,
        content=text, user_name="dain", user_timezone="UTC",
    )))


def _notes(db):
    with db() as s:
        return [(e.platform, e.event_metadata) for e in
                s.query(ConversationEvent).filter_by(kind="note").order_by(ConversationEvent.id)]


def test_switch_sends_one_notice_to_old_platform(db):
    deliver = FakeDeliver()
    orch = _orch(deliver)
    _say(orch, "hi from discord", "discord")
    _say(orch, "now on telegram!", "telegram")

    assert len(deliver.sent) == 1
    platform, pid, message = deliver.sent[0]
    assert platform == "discord"
    assert pid == "d-1"
    assert "telegram" in message  # names where the conversation went
    # the audit note landed on the old platform
    notes = _notes(db)
    assert len(notes) == 1
    assert notes[0][0] == "discord"
    assert notes[0][1]["note_type"] == "platform_switch"
    assert notes[0][1]["to"] == "telegram"


def test_no_repeat_while_staying_on_new_platform(db):
    deliver = FakeDeliver()
    orch = _orch(deliver)
    _say(orch, "hi", "discord")
    _say(orch, "switched", "telegram")
    _say(orch, "still here", "telegram")
    _say(orch, "and here", "telegram")

    assert len(deliver.sent) == 1  # only the switch moment


def test_switching_back_notifies_the_other_direction(db):
    deliver = FakeDeliver()
    orch = _orch(deliver)
    _say(orch, "hi", "discord")
    _say(orch, "switched", "telegram")      # notice -> discord
    _say(orch, "back again", "discord")     # notice -> telegram

    assert [(p) for p, _, _ in deliver.sent] == ["discord", "telegram"]


def test_first_ever_message_is_silent(db):
    deliver = FakeDeliver()
    orch = _orch(deliver)
    _say(orch, "very first message", "telegram")
    assert deliver.sent == []
    assert _notes(db) == []


def test_same_platform_is_silent(db):
    deliver = FakeDeliver()
    orch = _orch(deliver)
    _say(orch, "one", "discord")
    _say(orch, "two", "discord")
    assert deliver.sent == []


def test_inactive_old_link_is_skipped_entirely(db):
    """no note, no send - recording 'notice sent' without sending would lie."""
    deliver = FakeDeliver()
    orch = _orch(deliver)
    _say(orch, "hi", "discord")
    run(UserManager().deactivate_platform_identity("discord", "d-1"))
    _say(orch, "switched", "telegram")

    assert deliver.sent == []
    assert _notes(db) == []


def test_delivery_failure_is_non_fatal_and_not_retried(db):
    deliver = FakeDeliver(ok=False)
    orch = _orch(deliver)
    _say(orch, "hi", "discord")
    d = _say(orch, "switched", "telegram")

    assert d.text == "hi there!"           # the reply was unaffected
    assert len(deliver.sent) == 1          # one attempt
    assert len(_notes(db)) == 1            # note recorded regardless
    # ...and staying on telegram doesn't retry
    _say(orch, "still here", "telegram")
    assert len(deliver.sent) == 1


def test_no_deliver_callback_disables_the_feature(db):
    orch = _orch(None)
    _say(orch, "hi", "discord")
    d = _say(orch, "switched", "telegram")
    assert d.text == "hi there!"
    assert _notes(db) == []


def test_notice_is_invisible_to_scheduler_and_prompts(db):
    from src.managers.event_log import EventLog
    from src.personas import load_personas
    from src.services.prompt_service import PromptService

    deliver = FakeDeliver()
    orch = _orch(deliver)
    _say(orch, "hi", "discord")
    _say(orch, "switched", "telegram")

    log = EventLog("u1")
    # scheduler view: the last message is the agent's reply, not the note
    assert log.last_message().content == "hi there!"

    # prompt view: the note's text appears nowhere in rendered turns
    events = log.recent()
    ps = PromptService(persona=load_personas()["chordial"], enable_prompt_logging=False)
    req = run(ps.build_conversation_request(
        conversation_history=events, user_name="dain",
        user_uuid=None, user_timezone="UTC",
    ))
    joined = "".join(m.content for m in req.messages)
    assert "pssst" not in joined
