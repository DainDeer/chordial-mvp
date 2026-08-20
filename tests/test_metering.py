"""the meter (phase 7c): budgets that keep the council affordable without
ever lying about it. spend is a trailing-24h read of the usage ledger; the
soft budget silences proactive outreach (the cheapest lever), the hard
budget refuses conversational turns with honest ephemeral copy. both off
by default - an unmetered rig must behave byte-identically - and every
enforcement point fails OPEN: a broken meter never silences anyone.
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
from config import Config  # noqa: E402
from src.database.models import Base, UsageLog  # noqa: E402
from src.managers.helper_state_manager import HelperStateManager  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.models.unified_message import UnifiedMessage  # noqa: E402
from src.services import metering  # noqa: E402
from src.services.chat_service import BUDGET_REPLY, ChatService  # noqa: E402
from src.services.metering import (  # noqa: E402
    BudgetGate,
    BudgetVerdict,
    budget_verdict,
    spend_last_24h,
    usage_report,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


NOW = datetime(2026, 8, 19, 12, 0, 0)


def _log(session_factory, user_uuid, *, when, input_tokens=0,
         output_tokens=0, cache_read=0, cache_write=0,
         role="conversation", model="m", provider="anthropic"):
    with session_factory() as s:
        s.add(UsageLog(user_uuid=user_uuid, platform="app",
                       provider=provider, model=model, role=role,
                       input_tokens=input_tokens,
                       output_tokens=output_tokens,
                       cache_read_tokens=cache_read,
                       cache_write_tokens=cache_write,
                       created_at=when))
        s.commit()


# --- spend arithmetic --------------------------------------------------------


def test_spend_sums_the_trailing_window_only(db):
    _log(db, "u1", when=NOW - timedelta(hours=1),
         input_tokens=100, output_tokens=40, cache_read=500, cache_write=9)
    _log(db, "u1", when=NOW - timedelta(hours=23),
         input_tokens=10, output_tokens=5)
    # outside the window and someone else's spend: both invisible
    _log(db, "u1", when=NOW - timedelta(hours=25), input_tokens=9999)
    _log(db, "u2", when=NOW - timedelta(hours=1), input_tokens=7777)

    spend = spend_last_24h("u1", NOW)
    assert spend.input_tokens == 110
    assert spend.output_tokens == 45
    assert spend.cache_read_tokens == 500
    assert spend.cache_write_tokens == 9
    assert spend.calls == 2
    # budgeted = input + output; cache traffic deliberately doesn't count
    assert spend.budgeted == 155


def test_unmetered_default_never_touches_the_database(monkeypatch):
    # both knobs 0 (the default): verdict is clear without a query - the
    # spend reader raising proves it was never called
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 0)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 0)
    monkeypatch.setattr(metering, "spend_last_24h",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert budget_verdict("u1").state == "clear"


def test_verdict_thresholds(db, monkeypatch):
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 100)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 200)
    _log(db, "u1", when=NOW - timedelta(hours=1),
         input_tokens=50, output_tokens=40)
    assert budget_verdict("u1", NOW).state == "clear"

    _log(db, "u1", when=NOW - timedelta(hours=2), input_tokens=10)
    assert budget_verdict("u1", NOW).state == "soft"  # exactly at soft

    _log(db, "u1", when=NOW - timedelta(hours=3), input_tokens=100)
    verdict = budget_verdict("u1", NOW)
    assert verdict.state == "hard"
    assert verdict.spent == 200


def test_soft_only_and_hard_only_knobs_work_alone(db, monkeypatch):
    _log(db, "u1", when=NOW - timedelta(hours=1), input_tokens=150)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 100)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 0)
    assert budget_verdict("u1", NOW).state == "soft"
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 0)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 100)
    assert budget_verdict("u1", NOW).state == "hard"


def test_background_work_never_counts_against_the_user(db, monkeypatch):
    """sol's 7c round: curator/scorer/reconciler spend is the system's own
    cost. counting it wouldn't just miscount - the gates can't stop it,
    so it could push a user over the hard cap and HOLD them there, a
    silence with no escape. the turn-driven roles (conversation,
    scheduled, decider) are the whole budget."""
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 50)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 100)
    for role in ("curator", "scorer", "reconciler", "some_future_role"):
        _log(db, "u1", when=NOW - timedelta(hours=1),
             input_tokens=100_000, role=role)
    assert spend_last_24h("u1", NOW).budgeted == 0
    assert budget_verdict("u1", NOW).state == "clear"

    # the turn-driven roles all count
    _log(db, "u1", when=NOW - timedelta(hours=1), input_tokens=20,
         role="conversation")
    _log(db, "u1", when=NOW - timedelta(hours=1), input_tokens=20,
         role="scheduled")
    _log(db, "u1", when=NOW - timedelta(hours=1), input_tokens=20,
         role="decider")
    assert spend_last_24h("u1", NOW).budgeted == 60


def test_openai_cache_hits_are_normalized_out(db, monkeypatch):
    """sol's 7c round: openai reports cached tokens INSIDE input_tokens
    (anthropic keeps them in a separate field), so budgeting raw
    input+output would charge openai users for exactly the cache hits
    the budget promises never to count."""
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 0)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 500)
    # openai shape: 1000 input of which 900 were cache hits, 50 out
    _log(db, "u1", when=NOW - timedelta(hours=1), provider="openai",
         input_tokens=1000, output_tokens=50, cache_read=900)
    # anthropic shape: 200 (uncached) input, 300 cache reads besides
    _log(db, "u1", when=NOW - timedelta(hours=2), provider="anthropic",
         input_tokens=200, output_tokens=30, cache_read=300)

    spend = spend_last_24h("u1", NOW)
    # openai: 1000 - 900 + 50 = 150; anthropic: 200 + 30 = 230
    assert spend.budgeted == 380
    assert budget_verdict("u1", NOW).state == "clear"


# --- the soft gate (pulse) ---------------------------------------------------


class _Firing:
    class _Key:
        stream_id = "u1"
    key = _Key()
    kind = "scheduled_tick"


def test_gate_allows_within_budget(db, monkeypatch):
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 100)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 0)
    decision = run(BudgetGate().check(_Firing(), [], None))
    assert decision.allowed


def test_gate_denies_past_the_soft_budget(db, monkeypatch):
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 100)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 0)
    _log(db, "u1", when=datetime.utcnow() - timedelta(hours=1),
         input_tokens=150)
    decision = run(BudgetGate().check(_Firing(), [], None))
    assert not decision.allowed
    assert "budget" in decision.reason


def test_gate_fails_open_when_the_meter_breaks(monkeypatch):
    monkeypatch.setattr(metering, "budget_verdict",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
    decision = run(BudgetGate().check(_Firing(), [], None))
    assert decision.allowed
    assert "failing open" in decision.reason


# --- the hard gate (conversation) --------------------------------------------


class _NeverOrchestrator:
    async def handle(self, stimulus):  # pragma: no cover - the assertion
        raise AssertionError("a hard-budget turn must never reach the model")


def _msg(content="hello", platform_user_id="123"):
    return UnifiedMessage(content=content, platform_user_id=platform_user_id,
                          platform="discord", platform_message_id="m1",
                          metadata={"username": "tester"})


def test_hard_budget_refuses_the_turn_with_honest_copy(db):
    chat = ChatService(
        orchestrator=_NeverOrchestrator(),
        user_manager=UserManager(),
        budget_verdict=lambda u: BudgetVerdict("hard", 999, 100, 500),
    )
    assert run(chat.process_message(_msg())) == BUDGET_REPLY


def test_soft_budget_never_touches_conversation(db):
    # soft silences outreach only; a soft-state user still gets their turn
    chat = ChatService(
        orchestrator=None,  # echo fallback = the turn ran normally
        user_manager=UserManager(),
        budget_verdict=lambda u: BudgetVerdict("soft", 150, 100, 500),
    )
    user_manager = chat.user_manager
    user_uuid, _ = run(user_manager.get_or_create_user(
        "discord", "123", "tester"))
    run(HelperStateManager().set_status(user_uuid, "vel", "active"))
    run(user_manager.update_user_preferences(
        user_uuid, {"preferred_name": "tester"}))
    assert run(chat.process_message(_msg())) == "echo: hello"


def test_broken_meter_never_silences_a_conversation(db):
    def explode(user_uuid):
        raise RuntimeError("meter down")

    chat = ChatService(orchestrator=None, user_manager=UserManager(),
                       budget_verdict=explode)
    user_manager = chat.user_manager
    user_uuid, _ = run(user_manager.get_or_create_user(
        "discord", "123", "tester"))
    run(HelperStateManager().set_status(user_uuid, "vel", "active"))
    run(user_manager.update_user_preferences(
        user_uuid, {"preferred_name": "tester"}))
    assert run(chat.process_message(_msg())) == "echo: hello"


def test_budget_refusal_is_ephemeral(db):
    # the refusal never lands in the ledger of any kind - nothing to
    # assert on the event log here because the turn never starts; what
    # matters is that no usage row and no helper state changed
    chat = ChatService(
        orchestrator=_NeverOrchestrator(),
        user_manager=UserManager(),
        budget_verdict=lambda u: BudgetVerdict("hard", 999, 100, 500),
    )
    run(chat.process_message(_msg()))
    with db() as s:
        assert s.query(UsageLog).count() == 0


def test_concurrent_burst_cannot_bypass_the_hard_cap(db):
    """sol's 7c round: the check runs INSIDE the per-user lock. turns are
    serialized, so each check sees the spend its predecessor just
    recorded - a burst of simultaneous messages can't all pass on the
    same stale read and then generate anyway. here the first turn's
    spend crosses the cap: exactly one turn may run."""
    from dainframe.core import ActivationResult, LineResult

    class CountingOrchestrator:
        def __init__(self):
            self.calls = 0

        async def handle(self, stimulus):
            self.calls += 1
            return ActivationResult(
                activation_id=f"act-{self.calls}", stream_id="s",
                inbound_event_id=None, status="completed",
                status_reason=None,
                lines=(LineResult(line_id="l", speaker="vel",
                                  status="delivered"),))

    orchestrator = CountingOrchestrator()

    def verdict(user_uuid):
        # stands in for the ledger: once one turn has generated, its
        # recorded spend is past the hard cap
        state = "hard" if orchestrator.calls >= 1 else "clear"
        return BudgetVerdict(state, orchestrator.calls * 999, 100, 500)

    async def burst():
        chat = ChatService(orchestrator=orchestrator,
                           user_manager=UserManager(),
                           budget_verdict=verdict)
        user_uuid, _ = await chat.user_manager.get_or_create_user(
            "discord", "123", "tester")
        await HelperStateManager().set_status(user_uuid, "vel", "active")
        await chat.user_manager.update_user_preferences(
            user_uuid, {"preferred_name": "tester"})
        return await asyncio.gather(*(
            chat.process_message(_msg(f"m{i}")) for i in range(3)))

    replies = run(burst())
    assert orchestrator.calls == 1
    assert sum(1 for r in replies if r == BUDGET_REPLY) == 2


# --- the report --------------------------------------------------------------


def test_usage_report_aggregates_days_users_models_roles(db, monkeypatch):
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 100)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 0)
    user_manager = UserManager()
    u1, _ = run(user_manager.get_or_create_user("discord", "1", "a"))
    run(user_manager.update_user_preferences(u1, {"preferred_name": "megan"}))
    _log(db, u1, when=NOW - timedelta(hours=1), input_tokens=120,
         output_tokens=30, role="conversation", model="sonnet")
    _log(db, u1, when=NOW - timedelta(days=2), input_tokens=10,
         role="scheduled", model="haiku")
    _log(db, "ghost", when=NOW - timedelta(hours=2), input_tokens=5,
         model="haiku", role="decider")

    report = usage_report(days=14, now=NOW)
    assert report["budgets"] == {"soft": 100, "hard": 0}
    assert len(report["by_day"]) == 2
    assert {m["model"] for m in report["by_model"]} == {"sonnet", "haiku"}
    assert {r["role"] for r in report["by_role"]} == {
        "conversation", "scheduled", "decider"}

    top = report["users"][0]
    assert top["user_uuid"] == u1
    assert top["name"] == "megan"
    assert top["input"] == 130
    assert top["spent_24h"] == 150
    assert top["budget_state"] == "soft"
    assert top["last_call_at"].startswith("2026-08-19T11:00:00")


def test_usage_report_window_excludes_older_rows(db):
    _log(db, "u1", when=NOW - timedelta(days=20), input_tokens=999)
    report = usage_report(days=14, now=NOW)
    assert report["by_day"] == []
    assert report["users"] == []
