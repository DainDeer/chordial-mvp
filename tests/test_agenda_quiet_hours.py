"""the background agenda refresh must not poll notion during a user's quiet
hours - nobody's chatting overnight, so there's no reason to spend api calls
keeping the snapshot warm then. it should still refresh normally outside
quiet hours, and one 5-min cycle after quiet hours end (not wait for the full
ttl) so the digest is caught up well before a human says good morning.
"""
import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.scheduler_service import SchedulerService  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeAgendaService:
    def __init__(self):
        self.refreshed_uuids = []

    async def ensure_fresh(self, user_uuid):
        self.refreshed_uuids.append(user_uuid)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


def _make_user(db, timezone):
    with db() as s:
        user = User(preferred_name="tester", timezone=timezone)
        s.add(user)
        s.commit()
        return user.uuid


def test_refresh_skipped_during_quiet_hours(db, monkeypatch):
    # 6am utc = 1am new york -> within the default 21-8 quiet window
    fixed_utc = datetime(2026, 6, 15, 6, 0, 0)
    monkeypatch.setattr("src.services.scheduler_service.utc_now", lambda: fixed_utc)

    user_uuid = _make_user(db, "America/New_York")
    fake_agenda = FakeAgendaService()
    scheduler = SchedulerService(
        user_manager=UserManager(), agenda_service=fake_agenda,
    )

    run(scheduler._refresh_agenda(user_uuid))

    assert fake_agenda.refreshed_uuids == []


def test_refresh_runs_outside_quiet_hours(db, monkeypatch):
    # 6am utc = 3pm tokyo -> well outside quiet hours
    fixed_utc = datetime(2026, 6, 15, 6, 0, 0)
    monkeypatch.setattr("src.services.scheduler_service.utc_now", lambda: fixed_utc)

    user_uuid = _make_user(db, "Asia/Tokyo")
    fake_agenda = FakeAgendaService()
    scheduler = SchedulerService(
        user_manager=UserManager(), agenda_service=fake_agenda,
    )

    run(scheduler._refresh_agenda(user_uuid))

    assert fake_agenda.refreshed_uuids == [user_uuid]


def test_refresh_is_noop_without_agenda_service(db, monkeypatch):
    fixed_utc = datetime(2026, 6, 15, 6, 0, 0)
    monkeypatch.setattr("src.services.scheduler_service.utc_now", lambda: fixed_utc)

    user_uuid = _make_user(db, "Asia/Tokyo")
    scheduler = SchedulerService(user_manager=UserManager())  # no agenda_service

    run(scheduler._refresh_agenda(user_uuid))  # must not raise
