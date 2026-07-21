"""the live workspace agenda (replaces test_agenda_snapshot's coverage).

bucket partition against a fixed user timezone including the day-boundary
case, digest caps, and the digest-v2 sections (focus, plans by helper, wins
count, windows, occasions-within-3-days). notes must never appear anywhere.
"""
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
import src.services.workspace.agenda as agenda_mod
from src.database.models import Base, User
from src.services.workspace import get_store
from src.services.workspace.agenda import WorkspaceAgenda

U1 = "user-one"

# frozen "now": 2026-07-21 03:00 UTC == 2026-07-20 20:00 in LA - the day
# boundary case: the user's date is still the 20th while UTC is the 21st
FROZEN_UTC = datetime(2026, 7, 21, 3, 0, 0)
LA_TODAY = "2026-07-20"


@pytest.fixture()
def store(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(agenda_mod, "utc_now", lambda: FROZEN_UTC)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="dain", timezone="US/Pacific"))
        s.commit()
    yield get_store()
    engine.dispose()


def test_bucket_partition_respects_the_user_timezone(store):
    store.create_task(U1, "due today in LA", scheduled=LA_TODAY)
    store.create_task(U1, "overdue", scheduled="2026-07-19")
    t = store.create_task(U1, "started, undated")
    store.update_task(U1, t["id"], status="in_progress")
    store.create_task(U1, "future todo", scheduled="2026-07-25")

    payload = WorkspaceAgenda().get_payload(U1)
    assert [r["title"] for r in payload["tasks_today"]] == ["due today in LA"]
    assert [r["title"] for r in payload["tasks_overdue"]] == ["overdue"]
    assert [r["title"] for r in payload["tasks_in_progress"]] == ["started, undated"]
    # a future-dated To do is active but not part of the today picture
    all_rows = (payload["tasks_today"] + payload["tasks_overdue"]
                + payload["tasks_in_progress"])
    assert "future todo" not in [r["title"] for r in all_rows]


def test_closed_tasks_never_reach_the_payload(store):
    t = store.create_task(U1, "already done", scheduled=LA_TODAY)
    store.update_task(U1, t["id"], status="done")
    payload = WorkspaceAgenda().get_payload(U1)
    assert payload["tasks_today"] == []


def test_payload_rows_carry_public_ids_and_legacy_keys(store):
    plan = store.create_plan(U1, "finish the album", "aria", status="active")
    store.create_task(U1, "bounce stems", plan_id=plan["id"],
                      scheduled=LA_TODAY, window="afternoon", pom_estimate=2)
    row = WorkspaceAgenda().get_payload(U1)["tasks_today"][0]
    assert row["id"] == "t1"                      # public id - the reconciler echoes this
    assert row["status"] == "To do"               # display form, as notion rendered it
    assert row["project"] == "finish the album"   # legacy key name
    assert row["window"] == "afternoon" and row["pom"] == 2
    assert set(row) >= {"id", "title", "status", "priority", "scheduled",
                        "project", "cycle", "pom", "window", "helper"}


def test_digest_caps_overflow_into_and_more(store):
    for i in range(10):
        store.create_task(U1, f"task {i:02d}", scheduled=LA_TODAY)
    digest = WorkspaceAgenda().get_digest(U1)
    assert "today (10):" in digest
    assert "…and 2 more" in digest


def test_digest_v2_sections(store):
    plan = store.create_plan(U1, "finish the album", "aria", status="active")
    book = store.create_plan(U1, "write the novel", "poet", status="active")
    store.create_cycle(U1, "cycle 12", status="active",
                       start_date="2026-07-14", end_date="2026-07-27",
                       goal="ship it", focus="music first, words second")
    store.create_task(U1, "bounce stems", plan_id=plan["id"],
                      scheduled=LA_TODAY, window="afternoon", priority="high")
    store.log_win(U1, "backed up sessions", "2026-07-19", "aria", plan_id=plan["id"])
    store.log_win(U1, "wrote 400 words", "2026-07-16", "poet", plan_id=book["id"])
    store.create_occasion(U1, "dentist", "2026-07-22", time="14:30")

    digest = WorkspaceAgenda().get_digest(U1)
    assert digest.startswith("workspace agenda (background awareness")
    assert 'cycle: "cycle 12" ends jul 27 - focus: music first, words second' in digest
    assert '"bounce stems" [To do, high] (afternoon)' in digest
    assert "active plans: aria: \"finish the album\" / poet: \"write the novel\"" in digest
    assert "wins this week: 2" in digest
    assert 'coming up: "dentist" jul 22 14:30' in digest


def test_occasions_beyond_three_days_stay_out_of_the_digest(store):
    store.create_task(U1, "anchor", scheduled=LA_TODAY)   # keep digest non-empty
    store.create_occasion(U1, "too far away", "2026-07-26")
    digest = WorkspaceAgenda().get_digest(U1)
    assert "too far away" not in digest


def test_wins_older_than_a_week_dont_count(store):
    store.create_task(U1, "anchor", scheduled=LA_TODAY)
    store.log_win(U1, "ancient glory", "2026-07-01", "aria")
    assert "wins this week" not in WorkspaceAgenda().get_digest(U1)


def test_notes_never_appear_in_digest_or_payload(store):
    plan = store.create_plan(U1, "finish the album", "aria", status="active")
    store.jot(U1, "SECRET BRIDGE IDEA", plan_id=plan["id"], tags=["music"])
    agenda = WorkspaceAgenda()
    assert "SECRET BRIDGE IDEA" not in agenda.get_digest(U1)
    assert "SECRET BRIDGE IDEA" not in str(agenda.get_payload(U1))


def test_empty_workspace_yields_no_digest_at_all(store):
    assert WorkspaceAgenda().get_digest(U1) is None


def test_recurring_occasion_rolls_before_rendering(store):
    store.create_task(U1, "anchor", scheduled=LA_TODAY)
    store.create_occasion(U1, "weekly sync", "2026-07-15", recurrence="weekly")
    digest = WorkspaceAgenda().get_digest(U1)
    assert '"weekly sync" jul 22' in digest   # rolled past the 15th to the 22nd


def test_ensure_fresh_is_an_awaitable_noop(store):
    import asyncio
    assert asyncio.run(WorkspaceAgenda().ensure_fresh(U1)) is None
