"""notion tool tests with a fake client - no network, no api key required.

covers the three things most likely to break: property payloads match the
dainframe schema, name->relation resolution works, and list output is
formatted the way the model expects. follows the repo's plain-asyncio test
style (no pytest-asyncio dependency).
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

# make the project root importable (config.py + src live there)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("NOTION_API_KEY", "test-key")  # so get_client() constructs

from src.services.notion import client as notion_client  # noqa: E402
from src.services.notion import schema as S  # noqa: E402
from src.services.tools import notion_tools as NT  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """records create/update payloads and serves canned query results."""

    def __init__(self):
        self.created = []
        self.updated = []
        self._rows_by_db = {}

    def seed(self, db_id, rows):
        self._rows_by_db[db_id] = rows

    async def query_all(self, db_id, *, filter=None, sorts=None, limit=25):
        rows = self._rows_by_db.get(db_id, [])
        # honor a title-equals filter so resolution-by-title tests are exercised
        if filter and filter.get("property") in ("Task", "Project", "cycle") and "title" in filter:
            want = filter["title"]["equals"]
            rows = [r for r in rows if S.title_of(r, filter["property"]) == want]
        return rows[:limit]

    async def create_page(self, db_id, properties, children=None):
        self.created.append((db_id, properties))
        return {"id": "new-page-id", "properties": properties}

    async def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        return {"id": page_id, "properties": properties}


def _page(pid, title_prop, title):
    return {"id": pid, "properties": {title_prop: {"title": [{"plain_text": title}]}}}


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(NT, "get_client", lambda: fake)
    monkeypatch.setattr(notion_client, "_singleton", None)
    return fake


def test_create_task_builds_schema_correct_properties(fake_client):
    out = run(NT._create_task({"title": "write tests", "priority": "high"}, "u1"))
    assert "created task" in out
    db_id, props = fake_client.created[0]
    assert db_id == S.tasks_db()
    assert props["Task"]["title"][0]["text"]["content"] == "write tests"
    assert props["Status"]["status"]["name"] == "To do"  # default
    assert props["Priority"]["select"]["name"] == "high"


def test_create_task_resolves_project_name_to_relation(fake_client):
    fake_client.seed(S.projects_db(), [_page("proj-123", "Project", "chordial")])
    out = run(NT._create_task({"title": "ship notion", "project": "chordial"}, "u1"))
    assert "created task" in out
    _, props = fake_client.created[0]
    assert props["Project"]["relation"] == [{"id": "proj-123"}]


def test_create_task_unresolved_project_is_reported(fake_client):
    fake_client.seed(S.projects_db(), [])
    out = run(NT._create_task({"title": "x", "project": "ghost"}, "u1"))
    assert "couldn't find" in out and "ghost" in out
    assert not fake_client.created


def test_update_task_by_title_sets_status(fake_client):
    fake_client.seed(S.tasks_db(), [_page("task-9", "Task", "refill meds")])
    out = run(NT._update_task({"task": "refill meds", "status": "Done"}, "u1"))
    assert "updated task" in out
    page_id, props = fake_client.updated[0]
    assert page_id == "task-9"
    assert props["Status"]["status"]["name"] == "Done"


def test_update_task_no_fields_is_noop(fake_client):
    fake_client.seed(S.tasks_db(), [_page("task-9", "Task", "refill meds")])
    out = run(NT._update_task({"task": "refill meds"}, "u1"))
    assert "nothing to update" in out
    assert not fake_client.updated


def test_list_tasks_formats_with_relation_names(fake_client):
    task = {
        "id": "t1",
        "properties": {
            "Task": {"title": [{"plain_text": "do the thing"}]},
            "Status": {"status": {"name": "In progress"}},
            "Priority": {"select": {"name": "high"}},
            "Project": {"relation": [{"id": "proj-123"}]},
            "Sprint": {"relation": []},
            "Scheduled": {"date": {"start": "2026-07-06"}},
            "pom estimate": {"number": 2},
        },
    }
    fake_client.seed(S.tasks_db(), [task])
    fake_client.seed(S.projects_db(), [_page("proj-123", "Project", "chordial")])
    fake_client.seed(S.cycles_db(), [])
    out = run(NT._list_tasks({}, "u1"))
    assert "do the thing" in out
    assert "[In progress]" in out
    assert "project=chordial" in out
    assert "id=t1" in out


def test_create_cycle_builds_date_range_and_status(fake_client):
    out = run(NT._create_cycle(
        {"title": "Roe Deer", "start_date": "2026-07-06", "end_date": "2026-07-20", "goal": "ship"},
        "u1",
    ))
    assert "created cycle" in out
    _, props = fake_client.created[0]
    assert props["cycle"]["title"][0]["text"]["content"] == "Roe Deer"
    assert props["status"]["status"]["name"] == "Upcoming"
    assert props["dates"]["date"] == {"start": "2026-07-06", "end": "2026-07-20"}
    assert props["cycle goal"]["rich_text"][0]["text"]["content"] == "ship"


def test_looks_like_id_recognizes_uuids():
    assert NT._looks_like_id("9d5b5399-f284-481b-8d2a-e4797c6db18a")
    assert NT._looks_like_id("9d5b5399f284481b8d2ae4797c6db18a")
    assert not NT._looks_like_id("refill meds")
