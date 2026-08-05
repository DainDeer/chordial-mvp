"""the web focus view's api surface (src/web/server.py).

drives the real aiohttp app through aiohttp's TestClient against a throwaway
sqlite db: bucketing (today/overdue/in-progress), the start -> in_progress
composition, close-banks-the-clock, vocab passthrough on status, and the
user-resolution guardrails (ambiguous db = 409, never a guess).
"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, User
from src.services.workspace.focus import FocusStore
from src.services.workspace.store import WorkspaceStore
from src.web.server import WebService, resolve_web_user, WebUserError

U1 = "user-one"


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="dain", timezone="UTC"))
        s.commit()
    yield WorkspaceStore(), TestSession
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


async def _with_client(fn, user_resolver=None):
    service = WebService(user_resolver=user_resolver or (lambda: U1))
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def test_today_buckets(env):
    store, _ = env
    # the server's "today" is user-local (UTC for this fixture user), which
    # can differ from the test machine's local date - use the same reference
    from src.services.workspace.agenda import user_today
    today = user_today(U1)
    t_today = store.create_task(U1, "due today", scheduled=today.isoformat())
    t_over = store.create_task(
        U1, "slipped", scheduled=(today - timedelta(days=2)).isoformat())
    t_prog = store.create_task(U1, "ongoing", status="in_progress")
    store.create_task(U1, "future", scheduled=(today + timedelta(days=3)).isoformat())
    store.create_task(U1, "done already", status="done")

    async def scenario(client):
        res = await client.get("/api/today")
        assert res.status == 200
        body = await res.json()
        assert body["today"] == today.isoformat()
        assert body["user"]["name"] == "dain"
        assert body["pom_minutes"] > 0
        titles = {k: [t["title"] for t in v] for k, v in body["buckets"].items()}
        assert titles["today"] == ["due today"]
        assert titles["overdue"] == ["slipped"]
        assert titles["in_progress"] == ["ongoing"]
        # numeric ids ride along for the focus api
        assert body["buckets"]["today"][0]["id"] == t_today["id"]
        return body

    _run(_with_client(scenario))


def test_start_pause_complete_flow(env):
    store, _ = env
    task = store.create_task(U1, "record vocals")
    task_id = task["id"]

    async def scenario(client):
        # start: clock runs, task flips to in_progress
        res = await client.post("/api/focus/start", json={"task_id": task_id})
        assert res.status == 200
        body = await res.json()
        assert body["task"]["status"] == "in_progress"

        res = await client.get("/api/today")
        body = await res.json()
        assert body["focus"]["active_task_id"] == task_id

        # pause: no active task, seconds retained (>= 0)
        res = await client.post("/api/focus/pause")
        assert res.status == 200
        res = await client.get("/api/today")
        body = await res.json()
        assert body["focus"]["active_task_id"] is None

        # complete: legacy display vocab accepted, clock stays stopped
        res = await client.post(f"/api/tasks/{task_id}/status",
                                json={"status": "Done"})
        assert res.status == 200
        body = await res.json()
        assert body["task"]["status"] == "done"
        return body

    _run(_with_client(scenario))
    # the store agrees the task closed
    row = store.list_tasks(U1, include_closed=True)[0]
    assert row["status"] == "done"
    assert row["closed_at"] is not None


def test_completing_the_running_task_banks_the_clock(env, monkeypatch):
    store, _ = env
    task_id = store.create_task(U1, "sketch the chorus")["id"]

    from datetime import datetime
    import src.services.workspace.focus as focus_mod
    t0 = datetime(2026, 7, 28, 12, 0, 0)
    monkeypatch.setattr(focus_mod, "utc_now", lambda: t0)

    async def scenario(client):
        await client.post("/api/focus/start", json={"task_id": task_id})
        monkeypatch.setattr(focus_mod, "utc_now",
                            lambda: t0 + timedelta(minutes=7))
        res = await client.post(f"/api/tasks/{task_id}/status",
                                json={"status": "done"})
        assert res.status == 200

    _run(_with_client(scenario))
    snap = FocusStore().snapshot(U1)
    assert snap["active_task_id"] is None
    assert snap["seconds"][task_id] == 420


def test_error_shapes(env):
    async def scenario(client):
        res = await client.post("/api/focus/start", json={"task_id": 9999})
        assert res.status == 404
        res = await client.post("/api/focus/start", json={})
        assert res.status == 400
        res = await client.post("/api/tasks/1/status", json={"status": "someday"})
        assert res.status in (400, 404)

    _run(_with_client(scenario))


def test_user_resolution_guardrails(env):
    store, TestSession = env
    # one active real user resolves without config
    assert resolve_web_user() == U1
    # a second active user makes it ambiguous - refuse, don't guess
    with TestSession() as s:
        s.add(User(uuid="user-two", preferred_name="other"))
        s.commit()
    with pytest.raises(WebUserError, match="WEB_USER_UUID"):
        resolve_web_user()

    async def scenario(client):
        res = await client.get("/api/today")
        assert res.status == 409
        body = await res.json()
        assert "WEB_USER_UUID" in body["error"]

    _run(_with_client(scenario, user_resolver=resolve_web_user))


def test_frame_ancestors_header(env, monkeypatch):
    """every response (page, api, even redirects) declares who may frame it -
    the default is 'self'; deployments widen it for the portfolio desktop."""
    from config import Config

    async def scenario(client):
        for path in ("/", "/api/today"):
            res = await client.get(path)
            assert res.headers["Content-Security-Policy"] == \
                "frame-ancestors 'self'"

    _run(_with_client(scenario))

    monkeypatch.setattr(Config, "WEB_FRAME_ANCESTORS",
                        "'self' https://internetcreature.dev")

    async def widened(client):
        res = await client.get("/")
        assert res.headers["Content-Security-Policy"] == \
            "frame-ancestors 'self' https://internetcreature.dev"

    _run(_with_client(widened))


def test_no_cache_header(env):
    """html, api, and static responses all demand revalidation - a browser's
    heuristically-fresh copy of a pre-deploy page once masked a deploy (and
    its CSP fix) for days, and cloudflare edge-caches statics for hours."""
    async def scenario(client):
        for path in ("/", "/api/today", "/static/style.css"):
            res = await client.get(path)
            assert res.headers["Cache-Control"] == "no-cache", path

    _run(_with_client(scenario))
