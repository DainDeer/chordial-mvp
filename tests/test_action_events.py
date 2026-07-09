"""the tool-amnesia fix, locked down end to end.

the production incident: two quick turns each ran create_task for the same
notion task, because the model can't see its own tool calls from previous
turns (they lived only in the AgentTrace debug table). now successful mutating
calls persist as action events and replay into the prompt on the next turn.

the invariants:
- an action recorded in turn N appears in turn N+1's prompt, on a USER-side
  turn only (assistant turns stay verbatim - the PR #8 echo lesson)
- rendering is byte-stable: same events -> identical bytes, every time
- a message-only history renders byte-identically to the pre-actions format,
  so existing users' warm cache prefixes survive the deploy
- reads, errored calls, and refused turns persist nothing
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, ConversationEvent  # noqa: E402
from src.managers.event_log import Event, EventLog, format_action_line  # noqa: E402
from src.services.prompt_service import PromptService  # noqa: E402
from src.services.agent_service import ExecutedAction  # noqa: E402
from src.services.tools.base import Tool, ToolRegistry  # noqa: E402
from src.providers.ai.types import ToolDef  # noqa: E402


def run(coro):
    return asyncio.run(coro)


NOW = datetime(2026, 7, 7, 21, 41)  # 9:41pm utc


def _user(content, ts):
    return Event(author_type="user", author="user", kind="message",
                 content=content, created_at=ts)


def _agent(content, ts):
    return Event(author_type="agent", author="chordial", kind="message",
                 content=content, created_at=ts)


def _action(content, ts, author="chordial"):
    return Event(author_type="agent", author=author, kind="action",
                 content=content, created_at=ts)


def _svc():
    return PromptService(enable_prompt_logging=False)


ACTION_LINE = 'create_task {"scheduled_date": "2026-07-10", "title": "Look into VR fitness club discord schedule"} -> created task "Look into VR fitness club discord schedule" (id=22e0e094)'


# --- rendering: the incident, fixed ------------------------------------------

def test_action_from_prior_turn_is_visible_in_next_prompt():
    """turn 1 created a task; turn 2's prompt must show it, folded into the
    user-side turn that followed it."""
    history = [
        _user("hey can you add a task for the vr fitness schedule? friday", NOW - timedelta(minutes=1)),
        _action(ACTION_LINE, NOW - timedelta(seconds=55)),
        _agent("task's locked in! 💪", NOW - timedelta(seconds=50)),
        _user("oh also make sure it's the evening schedule", NOW),  # current turn
    ]
    req = run(_svc().build_conversation_request(
        conversation_history=history, user_name="dain",
        user_uuid=None, user_timezone="UTC",
    ))

    rendered = [m.content for m in req.messages]
    # the action line is present somewhere in the prompt
    assert any(ACTION_LINE in c for c in rendered)
    # ...but never on an assistant turn
    for m in req.messages:
        if m.role == "assistant":
            assert ACTION_LINE not in m.content
            assert m.content == "task's locked in! 💪"  # verbatim


def test_action_folds_into_next_user_turn_with_attribution():
    history = [
        _user("first", NOW - timedelta(minutes=10)),
        _agent("reply one", NOW - timedelta(minutes=9)),
        _action(ACTION_LINE, NOW - timedelta(minutes=8)),
        _user("second", NOW - timedelta(minutes=5)),
        _agent("reply two", NOW - timedelta(minutes=4)),
        _user("current", NOW),
    ]
    req = run(_svc().build_conversation_request(
        conversation_history=history, user_name="dain",
        user_uuid=None, user_timezone="UTC",
    ))
    # the "second" user turn carries the action block, before its timestamp line
    second_turn = req.messages[2]
    assert second_turn.role == "user"
    assert second_turn.content.startswith("[chordial's tool actions - ")
    assert ACTION_LINE in second_turn.content
    assert "] second" in second_turn.content


def test_trailing_actions_fold_into_current_conversation_turn():
    """actions with no following user message (e.g. run during the previous
    scheduled check-in) belong to the volatile current turn."""
    history = [
        _user("earlier", NOW - timedelta(hours=1)),
        _agent("i'll set that up for you!", NOW - timedelta(minutes=30)),
        _action(ACTION_LINE, NOW - timedelta(minutes=30)),
        _user("did you do it?", NOW),  # current
    ]
    req = run(_svc().build_conversation_request(
        conversation_history=history, user_name="dain",
        user_uuid=None, user_timezone="UTC",
    ))
    current = req.messages[-1].content
    assert current.startswith("[current time - ")
    assert ACTION_LINE in current
    assert current.endswith("did you do it?")
    # and no other turn carries it
    assert all(ACTION_LINE not in m.content for m in req.messages[:-1])


def test_trailing_actions_fold_into_scheduled_synthetic_turn():
    history = [
        _user("gn!", NOW - timedelta(hours=10)),
        _agent("sleep well 💜", NOW - timedelta(hours=10)),
        _action(ACTION_LINE, NOW - timedelta(minutes=3)),
    ]
    req = run(_svc().build_scheduled_request(
        conversation_history=history, user_name="dain",
        user_uuid=None, user_timezone="UTC",
    ))
    synthetic = req.messages[-1].content
    assert ACTION_LINE in synthetic
    assert "scheduled check-in" in synthetic


def test_consecutive_actions_group_into_one_block():
    history = [
        _user("set up my week", NOW - timedelta(minutes=2)),
        _action("create_task {\"title\": \"a\"} -> created (id=1)", NOW - timedelta(seconds=90)),
        _action("create_task {\"title\": \"b\"} -> created (id=2)", NOW - timedelta(seconds=85)),
        _agent("done, both queued!", NOW - timedelta(seconds=80)),
        _user("thanks!", NOW),
    ]
    req = run(_svc().build_conversation_request(
        conversation_history=history, user_name="dain",
        user_uuid=None, user_timezone="UTC",
    ))
    current = req.messages[-1].content
    assert current.count("chordial's tool actions") == 1  # one block, two lines
    assert "(id=1)" in current and "(id=2)" in current


# --- byte stability -----------------------------------------------------------

def test_rendering_is_deterministic():
    history = [
        _user("hi", NOW - timedelta(minutes=5)),
        _action(ACTION_LINE, NOW - timedelta(minutes=4)),
        _user("again", NOW),
    ]
    r1 = run(_svc().build_conversation_request(
        conversation_history=history, user_name="dain", user_uuid=None, user_timezone="UTC"))
    r2 = run(_svc().build_conversation_request(
        conversation_history=history, user_name="dain", user_uuid=None, user_timezone="UTC"))
    assert [m.content for m in r1.messages[:-1]] == [m.content for m in r2.messages[:-1]]


def test_message_only_history_renders_preactions_format():
    """no action events -> byte-identical to the pre-actions rendering, so
    existing users' warm cache prefixes survive the deploy: user turns are
    exactly '[<ts>] <text>', assistant turns exactly verbatim."""
    ts = datetime(2026, 7, 7, 9, 41)
    history = [
        _user("hello", ts),
        _agent("hi there!", ts + timedelta(minutes=1)),
        _user("current", NOW),
    ]
    req = run(_svc().build_conversation_request(
        conversation_history=history, user_name="dain",
        user_uuid=None, user_timezone="UTC",
    ))
    assert req.messages[0].content == "[tue jul 07 9:41am] hello"
    assert req.messages[1].content == "hi there!"
    assert req.messages[-1].content.startswith("[current time - ")
    assert req.messages[-1].content.endswith("current")
    assert "tool actions" not in "".join(m.content for m in req.messages)


# --- the frozen action line ---------------------------------------------------

def test_format_action_line_is_deterministic_and_capped():
    line1 = format_action_line("create_task", {"b": 1, "a": 2}, "made it")
    line2 = format_action_line("create_task", {"a": 2, "b": 1}, "made it")
    assert line1 == line2  # sorted keys
    assert line1 == 'create_task {"a": 2, "b": 1} -> made it'

    long_result = "x" * 1000
    capped = format_action_line("t", {}, long_result)
    assert len(capped) < 400
    assert capped.endswith("…")

    multiline = format_action_line("t", {}, "line one\nline two\n  spaced")
    assert "\n" not in multiline.split(" -> ", 1)[1]  # result flattened to one line


# --- recording policy (db-backed) ----------------------------------------------

@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain"))
        s.commit()
    yield TestSession
    engine.dispose()


def _registry():
    reg = ToolRegistry()

    async def _noop(tool_input, user_uuid):
        return "ok"

    reg.register(Tool(
        definition=ToolDef(name="create_task", description="", input_schema={}),
        handler=_noop))
    reg.register(Tool(
        definition=ToolDef(name="list_tasks", description="", input_schema={}),
        handler=_noop, record_event=False))
    return reg


class FakeAgent:
    """returns a canned AgentOutcome; lets us drive the orchestrator's recording."""
    name = "chordial"

    def __init__(self, outcome):
        self._outcome = outcome

    async def act(self, briefing):
        return self._outcome


def _orchestrator(outcome):
    from src.managers.user_manager import UserManager
    from src.services.orchestrator import Orchestrator
    return Orchestrator(
        agents={"chordial": FakeAgent(outcome)},
        user_manager=UserManager(),
        tool_registry=_registry(),
    )


def _stimulus(content="hey"):
    from src.services.orchestrator import Stimulus
    return Stimulus(kind="user_message", user_uuid="u1", platform="discord",
                    content=content, user_name="dain", user_timezone="UTC")


def _events(db):
    with db() as s:
        return [(e.kind, e.author, e.content) for e in
                s.query(ConversationEvent).order_by(ConversationEvent.id).all()]


def _outcome(text="done!", actions=(), refused=False):
    from src.agents.base import AgentOutcome
    return AgentOutcome(text=text, actions=list(actions), refused=refused)


def test_successful_mutation_is_recorded_before_reply(db):
    orch = _orchestrator(_outcome(actions=[
        ExecutedAction("create_task", {"title": "x"}, 'created task "x" (id=1)', False, False),
    ]))
    run(orch.handle(_stimulus()))

    events = _events(db)
    kinds = [k for k, _, _ in events]
    assert kinds == ["message", "action", "message"]  # user -> action -> reply
    assert events[1][1] == "chordial"
    assert 'create_task {"title": "x"} -> created task "x" (id=1)' == events[1][2]


def test_reads_and_errors_are_not_recorded(db):
    orch = _orchestrator(_outcome(actions=[
        ExecutedAction("list_tasks", {}, "3 tasks", False, False),          # read: skip
        ExecutedAction("create_task", {"title": "y"}, "boom", True, False), # error: skip
    ]))
    run(orch.handle(_stimulus()))

    kinds = [k for k, _, _ in _events(db)]
    assert kinds == ["message", "message"]  # no action events at all


def test_refused_turn_persists_nothing_after_user_message(db):
    orch = _orchestrator(_outcome(text=None, refused=True, actions=[
        ExecutedAction("create_task", {"title": "z"}, "created", False, False),
    ]))
    deliverable = run(orch.handle(_stimulus()))

    assert deliverable.refused is True
    kinds = [k for k, _, _ in _events(db)]
    assert kinds == ["message"]  # only the inbound user message


def test_scheduler_ignores_trailing_action_event(db):
    """a tool action recorded after the reply must not reset the scheduler's
    'last message' clock or role."""
    from src.services.scheduler_service import SchedulerService
    from src.managers.user_manager import UserManager

    log = EventLog("u1", "discord")
    log.append_message("agent", "chordial", "checking in~", message_type="scheduled")
    log.append_action("chordial", "create_task", {"title": "x"}, "created")

    scheduler = SchedulerService(user_manager=UserManager())
    role, _, mtype = run(scheduler._check_last_message("u1", "discord"))
    assert (role, mtype) == ("assistant", "scheduled")
