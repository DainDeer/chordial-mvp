"""agenda snapshot tests: the notion->db refresh, the freshness policy, the
digest render, write-tool invalidation, and the cache-safe prompt injection.

a fake notion client (no network) serves canned pages; an isolated temp db backs
the snapshot rows. plain-asyncio style, matching the rest of the suite.
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("NOTION_API_KEY", "test-key")  # so agenda_enabled() is true

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, AgendaSnapshot  # noqa: E402
from src.services.notion import schema as S  # noqa: E402
from src.services.notion.client import NotionError  # noqa: E402
from src.services.notion import snapshot_service as snap  # noqa: E402
from src.services.notion.snapshot_service import AgendaSnapshotService, invalidate_all  # noqa: E402
from src.utils.timezone_utils import utc_now  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- fakes -----------------------------------------------------------------

class FakeClient:
    """serves canned query results by db id and counts queries."""

    def __init__(self):
        self._rows_by_db = {}
        self.query_count = 0
        self.raise_error = False

    def seed(self, db_id, rows):
        self._rows_by_db[db_id] = rows

    async def query_all(self, db_id, *, filter=None, sorts=None, limit=25):
        self.query_count += 1
        if self.raise_error:
            raise NotionError("boom", status=503)
        return list(self._rows_by_db.get(db_id, []))[:limit]


def _task(pid, title, *, status="To do", priority=None, scheduled=None,
          project_rel=None, sprint_rel=None):
    props = {
        "Task": {"title": [{"plain_text": title}]},
        "Status": {"status": {"name": status}},
        "Priority": {"select": {"name": priority}} if priority else {"select": None},
        "Project": {"relation": [{"id": project_rel}] if project_rel else []},
        "Sprint": {"relation": [{"id": sprint_rel}] if sprint_rel else []},
        "Scheduled": {"date": {"start": scheduled}} if scheduled else {"date": None},
    }
    return {"id": pid, "properties": props}


def _cycle(pid, title, *, status="Active", dates=None, goal=None):
    props = {
        "cycle": {"title": [{"plain_text": title}]},
        "status": {"status": {"name": status}},
        "dates": {"date": dates} if dates else {"date": None},
        "cycle goal": {"rich_text": [{"plain_text": goal}]} if goal else {"rich_text": []},
    }
    return {"id": pid, "properties": props}


def _project(pid, title, *, areas=None):
    props = {
        "Project": {"title": [{"plain_text": title}]},
        "Area": {"multi_select": [{"name": a} for a in (areas or [])]},
    }
    return {"id": pid, "properties": props}


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain", timezone="US/Pacific"))
        s.commit()
    yield TestSession
    os.close(fd)
    os.unlink(path)


def _service(fake):
    return AgendaSnapshotService(ttl_minutes=30, client_factory=lambda: fake)


def _today_iso():
    # the service uses the user's local date (US/Pacific); build "today" the
    # same way so the seeded dates land in the buckets we expect.
    from src.utils.timezone_utils import to_user_timezone
    return to_user_timezone(utc_now(), "US/Pacific").date().isoformat()


def _days_from_today(delta_days):
    from datetime import date
    return (date.fromisoformat(_today_iso()) + timedelta(days=delta_days)).isoformat()


# --- refresh + partitioning ------------------------------------------------

def test_refresh_partitions_tasks_into_today_overdue_in_progress(db):
    today = _today_iso()
    yesterday = _days_from_today(-1)
    fake = FakeClient()
    fake.seed(S.tasks_db(), [
        _task("t-today", "write design doc", status="In progress", priority="high", scheduled=today),
        _task("t-old", "renew passport", status="To do", scheduled=yesterday),
        _task("t-wip", "mixdown track 3", status="In progress", project_rel="proj-1"),
    ])
    fake.seed(S.cycles_db(), [_cycle("cyc-1", "sika deer", dates={"start": today, "end": _days_from_today(7)}, goal="ship notion awareness")])
    fake.seed(S.projects_db(), [_project("proj-1", "music tools", areas=["music"])])

    payload = run(_service(fake).refresh("u1"))

    assert [t["id"] for t in payload["tasks_today"]] == ["t-today"]
    assert [t["id"] for t in payload["tasks_overdue"]] == ["t-old"]
    assert [t["id"] for t in payload["tasks_in_progress"]] == ["t-wip"]
    # relation resolved from the in-progress projects we already fetched
    assert payload["tasks_in_progress"][0]["project"] == "music tools"
    assert payload["cycle"]["title"] == "sika deer"


def test_refresh_writes_digest_and_marks_fresh(db):
    today = _today_iso()
    fake = FakeClient()
    fake.seed(S.tasks_db(), [_task("t1", "book dentist", scheduled=today)])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    svc = _service(fake)

    run(svc.refresh("u1"))

    with db() as s:
        row = s.query(AgendaSnapshot).filter_by(user_uuid="u1").one()
    assert row.is_stale is False
    assert row.refreshed_at is not None
    assert "book dentist" in row.digest
    assert row.digest == svc.get_digest("u1")


def test_digest_caps_and_counts(db):
    today = _today_iso()
    fake = FakeClient()
    fake.seed(S.tasks_db(), [_task(f"t{i}", f"task {i}", scheduled=today) for i in range(12)])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    svc = _service(fake)

    run(svc.refresh("u1"))
    digest = svc.get_digest("u1")

    assert "today (12):" in digest      # count reflects the full set
    assert "…and 4 more" in digest      # but only 8 are rendered (cap)


def test_empty_agenda_renders_clear_line(db):
    fake = FakeClient()
    fake.seed(S.tasks_db(), [])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])

    payload = run(_service(fake).refresh("u1"))
    assert payload["tasks_today"] == []
    assert _service(fake).get_digest("u1") == "notion agenda: clear - nothing scheduled today, nothing overdue."


# --- freshness policy ------------------------------------------------------

def test_ensure_fresh_skips_when_fresh(db):
    fake = FakeClient()
    fake.seed(S.tasks_db(), [])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    svc = _service(fake)

    run(svc.ensure_fresh("u1"))     # first call refreshes (3 queries)
    assert fake.query_count == 3
    run(svc.ensure_fresh("u1"))     # still fresh -> no new queries
    assert fake.query_count == 3


def test_ensure_fresh_refreshes_when_stale(db):
    fake = FakeClient()
    fake.seed(S.tasks_db(), [])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    svc = _service(fake)

    run(svc.ensure_fresh("u1"))
    invalidate_all()                # a notion write flags it stale
    run(svc.ensure_fresh("u1"))
    assert fake.query_count == 6    # refreshed again


def test_ensure_fresh_refreshes_when_past_ttl(db):
    fake = FakeClient()
    fake.seed(S.tasks_db(), [])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    svc = _service(fake)

    run(svc.ensure_fresh("u1"))
    # backdate the refresh past the 30-min ttl
    with db() as s:
        row = s.query(AgendaSnapshot).filter_by(user_uuid="u1").one()
        row.refreshed_at = utc_now() - timedelta(minutes=31)
        s.commit()
    run(svc.ensure_fresh("u1"))
    assert fake.query_count == 6


# --- error handling --------------------------------------------------------

def test_notion_error_keeps_last_good_digest(db):
    today = _today_iso()
    fake = FakeClient()
    fake.seed(S.tasks_db(), [_task("t1", "good task", scheduled=today)])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    svc = _service(fake)

    run(svc.refresh("u1"))
    good_digest = svc.get_digest("u1")
    assert "good task" in good_digest

    fake.raise_error = True
    result = run(svc.refresh("u1"))
    assert result is None                    # refresh reported failure
    assert svc.get_digest("u1") == good_digest  # but the digest survived

    with db() as s:
        row = s.query(AgendaSnapshot).filter_by(user_uuid="u1").one()
    assert row.is_stale is True
    assert row.last_error


# --- invalidation ----------------------------------------------------------

def test_invalidate_all_flags_every_row_stale(db):
    fake = FakeClient()
    fake.seed(S.tasks_db(), [])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    run(_service(fake).refresh("u1"))

    with db() as s:
        assert s.query(AgendaSnapshot).filter_by(user_uuid="u1").one().is_stale is False

    invalidate_all()

    with db() as s:
        assert s.query(AgendaSnapshot).filter_by(user_uuid="u1").one().is_stale is True


def test_write_tool_invalidates_snapshot(db, monkeypatch):
    """creating a task through the notion tool flags the snapshot stale."""
    from src.services.tools import notion_tools as NT
    fake = FakeClient()
    fake.seed(S.tasks_db(), [])
    fake.seed(S.cycles_db(), [])
    fake.seed(S.projects_db(), [])
    run(_service(fake).refresh("u1"))

    # a minimal fake client for the *tool*, separate from ours. create_task
    # also runs a duplicate-check query first, so serve that too (no dupes).
    class ToolClient:
        async def query_all(self, db_id, *, filter=None, sorts=None, limit=25):
            return []
        async def create_page(self, db_id, properties, children=None):
            return {"id": "new", "properties": properties}
    monkeypatch.setattr(NT, "get_client", lambda: ToolClient())

    run(NT._create_task({"title": "new todo"}, "u1"))

    with db() as s:
        assert s.query(AgendaSnapshot).filter_by(user_uuid="u1").one().is_stale is True


def test_get_digest_is_none_when_no_snapshot(db):
    fake = FakeClient()
    assert _service(fake).get_digest("nobody") is None
