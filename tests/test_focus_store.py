"""FocusStore invariants (the web focus view's pomodoro clock).

covers: banking on switch (the feature's whole point - progress saves when
switching task), the at-most-one-running invariant, idempotent pause, the
completion-path stop, closed/foreign-task refusal, and live-delta snapshots.
the clock is frozen by monkeypatching utc_now in the focus module, same
technique the pulse tests use - no sleeps.
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
import src.services.workspace.focus as focus_mod
from src.database.models import Base, User
from src.services.workspace.focus import FocusStore
from src.services.workspace.store import WorkspaceStore

U1, U2 = "user-one", "user-two"
T0 = datetime(2026, 7, 28, 12, 0, 0)


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="dain"))
        s.add(User(uuid=U2, preferred_name="other"))
        s.commit()
    yield WorkspaceStore(), FocusStore()
    engine.dispose()


def _clock(monkeypatch, at: datetime):
    monkeypatch.setattr(focus_mod, "utc_now", lambda: at)


def test_start_switch_banks_progress(env, monkeypatch):
    """switching tasks banks the outgoing task's time and keeps it."""
    store, focus = env
    a = store.create_task(U1, "write the bridge")["id"]
    b = store.create_task(U1, "practice scales")["id"]

    _clock(monkeypatch, T0)
    snap = focus.start(U1, a)
    assert snap["active_task_id"] == a

    # 10 minutes later, switch to b
    _clock(monkeypatch, T0 + timedelta(minutes=10))
    snap = focus.start(U1, b)
    assert snap["active_task_id"] == b
    assert snap["seconds"][a] == 600          # banked, not lost
    assert snap["seconds"][b] == 0

    # 5 more minutes, switch back to a - it resumes on top of its bank
    _clock(monkeypatch, T0 + timedelta(minutes=15))
    snap = focus.start(U1, a)
    assert snap["active_task_id"] == a
    assert snap["seconds"][b] == 300

    _clock(monkeypatch, T0 + timedelta(minutes=20))
    snap = focus.snapshot(U1)
    assert snap["seconds"][a] == 900          # 600 banked + 300 live


def test_snapshot_includes_live_delta(env, monkeypatch):
    store, focus = env
    a = store.create_task(U1, "mix the demo")["id"]
    _clock(monkeypatch, T0)
    focus.start(U1, a)
    _clock(monkeypatch, T0 + timedelta(seconds=90))
    snap = focus.snapshot(U1)
    assert snap["active_task_id"] == a
    assert snap["seconds"][a] == 90


def test_pause_banks_and_is_idempotent(env, monkeypatch):
    store, focus = env
    a = store.create_task(U1, "walk the dog")["id"]
    _clock(monkeypatch, T0)
    focus.start(U1, a)
    _clock(monkeypatch, T0 + timedelta(minutes=3))
    snap = focus.pause(U1)
    assert snap["active_task_id"] is None
    assert snap["seconds"][a] == 180
    # pausing again changes nothing
    _clock(monkeypatch, T0 + timedelta(minutes=30))
    snap = focus.pause(U1)
    assert snap["seconds"][a] == 180


def test_stop_only_touches_the_named_running_task(env, monkeypatch):
    store, focus = env
    a = store.create_task(U1, "task a")["id"]
    b = store.create_task(U1, "task b")["id"]
    _clock(monkeypatch, T0)
    focus.start(U1, a)
    # stopping b (not running) is a no-op; a keeps running
    focus.stop(U1, b)
    _clock(monkeypatch, T0 + timedelta(minutes=2))
    assert focus.snapshot(U1)["active_task_id"] == a
    # the completion path: stop a specifically
    focus.stop(U1, a)
    snap = focus.snapshot(U1)
    assert snap["active_task_id"] is None
    assert snap["seconds"][a] == 120


def test_start_refuses_closed_and_foreign_tasks(env):
    store, focus = env
    done = store.create_task(U1, "already finished", status="done")["id"]
    with pytest.raises(ValueError, match="closed"):
        focus.start(U1, done)
    theirs = store.create_task(U2, "someone else's work")["id"]
    with pytest.raises(ValueError, match="not found"):
        focus.start(U1, theirs)


def test_at_most_one_running_per_user_but_users_are_independent(env, monkeypatch):
    store, focus = env
    a = store.create_task(U1, "mine")["id"]
    b = store.create_task(U2, "theirs")["id"]
    _clock(monkeypatch, T0)
    focus.start(U1, a)
    focus.start(U2, b)
    assert focus.snapshot(U1)["active_task_id"] == a
    assert focus.snapshot(U2)["active_task_id"] == b
    # U2's clock never shows up in U1's snapshot
    assert b not in focus.snapshot(U1)["seconds"]
