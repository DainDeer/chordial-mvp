"""completion-reconciler tests: the executor and gating, plus its wiring into
the orchestrator.

the model's judgment (generic-activity vs specific-deliverable) lives in the
prompt and isn't unit-testable deterministically, so a scripted fake provider
stands in for it. what these lock down is the machinery around it: open tasks
are gathered from the agenda snapshot, no open tasks means no llm call, only
genuinely-open ids get marked Done (hallucinated ids are rejected), json is
parsed tolerantly, and the orchestrator records the Done marks as chordial's
own actions after a user turn.
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
from src.database.models import Base, User, ConversationEvent  # noqa: E402
from src.services.completion_reconciler import (  # noqa: E402
    CompletionReconcilerService, RECONCILER_SYSTEM,
)
from src.services.tools.base import Tool, ToolRegistry  # noqa: E402
from src.providers.ai.types import AIResponse, ChatTurn, Usage, ToolDef  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeProvider:
    model = "fake-utility"

    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.calls = 0

    async def create_message(self, request):
        self.calls += 1
        return AIResponse(
            text=self.reply_text, tool_calls=[], stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
            assistant_turn=ChatTurn(role="assistant", content=self.reply_text),
        )


class FakeAgenda:
    def __init__(self, payload):
        self._payload = payload

    def get_payload(self, user_uuid):
        return self._payload


class NoUsage:
    def record_call(self, **k): pass
    def record_trace(self, **k): pass


def _registry(record):
    """a registry whose update_task records (task_id, status) into `record`."""
    reg = ToolRegistry()

    async def _update_task(tool_input, user_uuid):
        record.append((tool_input.get("task"), tool_input.get("status")))
        return f'updated task (id={tool_input.get("task")}): Status.'

    reg.register(Tool(
        definition=ToolDef(name="update_task", description="", input_schema={}),
        handler=_update_task,
    ))
    return reg


def _payload(*open_titles):
    """agenda payload with the given (id, title) tasks in tasks_today."""
    return {
        "cycle": None, "projects": [],
        "tasks_today": [{"id": tid, "title": title, "status": "To do"}
                        for tid, title in open_titles],
        "tasks_overdue": [], "tasks_in_progress": [], "done_today": [],
    }


def _service(reply, payload, record):
    return CompletionReconcilerService(
        provider=FakeProvider(reply),
        provider_name="fake",
        agenda_service=FakeAgenda(payload),
        tool_registry=_registry(record),
        usage_recorder=NoUsage(),
    )


def test_marks_a_reported_task_done():
    record = []
    svc = _service('{"completed": [{"id": "piano-1", "why": "practiced chords"}]}',
                   _payload(("piano-1", "practice piano"), ("walk-1", "go for a walk")),
                   record)
    result = run(svc.reconcile("u1", "discord", "i practiced chords in c and walked!"))
    assert record == [("piano-1", "Done")]
    assert len(result.actions) == 1
    assert result.actions[0].name == "update_task"
    assert result.considered == 2


def test_no_open_tasks_means_no_llm_call():
    record = []
    provider = FakeProvider('{"completed": []}')
    svc = CompletionReconcilerService(
        provider=provider, provider_name="fake",
        agenda_service=FakeAgenda(_payload()),  # empty
        tool_registry=_registry(record), usage_recorder=NoUsage(),
    )
    result = run(svc.reconcile("u1", "discord", "i did a bunch of stuff"))
    assert provider.calls == 0   # skipped entirely
    assert result.actions == []
    assert record == []


def test_empty_message_means_no_llm_call():
    record = []
    provider = FakeProvider('{"completed": []}')
    svc = CompletionReconcilerService(
        provider=provider, provider_name="fake",
        agenda_service=FakeAgenda(_payload(("piano-1", "practice piano"))),
        tool_registry=_registry(record), usage_recorder=NoUsage(),
    )
    run(svc.reconcile("u1", "discord", "   "))
    assert provider.calls == 0


def test_hallucinated_id_is_rejected_not_executed():
    record = []
    svc = _service('{"completed": [{"id": "ghost-9"}]}',
                   _payload(("piano-1", "practice piano")), record)
    result = run(svc.reconcile("u1", "discord", "did some stuff"))
    assert record == []                    # nothing written
    assert result.actions == []
    assert result.rejected and result.rejected[0]["id"] == "ghost-9"


def test_empty_completed_list_marks_nothing():
    record = []
    svc = _service('{"completed": []}', _payload(("piano-1", "practice piano")), record)
    result = run(svc.reconcile("u1", "discord", "just feeling a bit tired today"))
    assert record == []
    assert result.actions == []


def test_json_in_code_fence_is_parsed():
    record = []
    svc = _service('```json\n{"completed": [{"id": "piano-1"}]}\n```',
                   _payload(("piano-1", "practice piano")), record)
    run(svc.reconcile("u1", "discord", "played piano"))
    assert record == [("piano-1", "Done")]


def test_duplicate_ids_marked_once():
    record = []
    svc = _service('{"completed": [{"id": "piano-1"}, {"id": "piano-1"}]}',
                   _payload(("piano-1", "practice piano")), record)
    run(svc.reconcile("u1", "discord", "piano piano piano"))
    assert record == [("piano-1", "Done")]


def test_gathers_open_tasks_from_all_buckets():
    record = []
    payload = {
        "tasks_today": [{"id": "a", "title": "today task", "status": "To do"}],
        "tasks_overdue": [{"id": "b", "title": "overdue task", "status": "To do"}],
        "tasks_in_progress": [{"id": "c", "title": "wip task", "status": "In progress"}],
    }
    svc = _service('{"completed": [{"id": "b"}, {"id": "c"}]}', payload, record)
    result = run(svc.reconcile("u1", "discord", "knocked out the overdue one and the wip"))
    assert result.considered == 3
    assert record == [("b", "Done"), ("c", "Done")]


def test_system_prompt_encodes_the_generic_activity_rule():
    # the owner's key nuance must live in the reconciler's instructions
    assert "generic activit" in RECONCILER_SYSTEM.lower()
    assert "in passing" in RECONCILER_SYSTEM.lower()


# --- orchestrator integration -------------------------------------------------

@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


def test_orchestrator_records_reconciled_marks_as_chordial_actions(db):
    from src.agents.base import AgentOutcome
    from src.services.orchestrator import Orchestrator, Stimulus

    class FakeCompanion:
        name = "chordial"
        async def act(self, briefing):
            return AgentOutcome(text="nice, sounds like a good day 🦌")

    record = []
    reconciler = _service('{"completed": [{"id": "piano-1"}]}',
                          _payload(("piano-1", "practice piano")), record)
    orch = Orchestrator(
        agents={"chordial": FakeCompanion()},
        user_manager=__import__("src.managers.user_manager", fromlist=["UserManager"]).UserManager(),
        reconciler=reconciler,
    )
    run(orch.handle(Stimulus(kind="user_message", user_uuid="u1", platform="discord",
                             content="i practiced piano and went for a walk :3",
                             user_name="dain", user_timezone="UTC")))

    # the task got marked, and the mark is recorded as chordial's own action,
    # after the reply
    assert record == [("piano-1", "Done")]
    with db() as s:
        events = [(e.kind, e.author, e.content[:30]) for e in
                  s.query(ConversationEvent).order_by(ConversationEvent.id).all()]
    kinds = [k for k, _, _ in events]
    assert kinds == ["message", "message", "action"]  # user, reply, then the Done mark
    assert events[2][1] == "chordial"
    assert "update_task" in events[2][2]


def test_reconciler_does_not_run_on_scheduled_tick(db):
    from src.agents.base import AgentOutcome
    from src.services.orchestrator import Orchestrator, Stimulus

    class FakeCompanion:
        name = "chordial"
        async def act(self, briefing):
            return AgentOutcome(text="evening check-in!")

    record = []
    provider = FakeProvider('{"completed": [{"id": "piano-1"}]}')
    reconciler = CompletionReconcilerService(
        provider=provider, provider_name="fake",
        agenda_service=FakeAgenda(_payload(("piano-1", "practice piano"))),
        tool_registry=_registry(record), usage_recorder=NoUsage(),
    )
    orch = Orchestrator(
        agents={"chordial": FakeCompanion()},
        user_manager=__import__("src.managers.user_manager", fromlist=["UserManager"]).UserManager(),
        reconciler=reconciler,
    )
    run(orch.handle(Stimulus(kind="scheduled_tick", user_uuid="u1", platform="discord")))
    assert provider.calls == 0   # reconciler only runs on user messages
    assert record == []
