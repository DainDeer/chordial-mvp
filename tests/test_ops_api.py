"""the operator's meter endpoint (phase 7c): /ops + /api/ops/usage.

the routes exist only when OPS_TOKEN is configured (same pattern as the
update feed - an unconfigured deployment has nothing to probe), the data
endpoint refuses anything but the exact bearer token in constant time, and
the payload is the same arithmetic the gates enforce - the dashboard can
never disagree with the behavior.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from config import Config
from src.database.models import Base, UsageLog, User
from src.web.server import WebService

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
        s.add(User(uuid=U1, preferred_name="megan", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


async def _with_client(fn):
    service = WebService()
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def _log(session_factory, user_uuid, *, input_tokens=0, output_tokens=0,
         hours_ago=1, role="conversation", model="m"):
    with session_factory() as s:
        s.add(UsageLog(user_uuid=user_uuid, platform="app",
                       provider="anthropic", model=model, role=role,
                       input_tokens=input_tokens,
                       output_tokens=output_tokens,
                       created_at=datetime.utcnow()
                       - timedelta(hours=hours_ago)))
        s.commit()


def test_routes_absent_without_ops_token(env, monkeypatch):
    monkeypatch.setattr(Config, "OPS_TOKEN", None)

    async def flow(client):
        assert (await client.get("/ops")).status == 404
        assert (await client.get("/api/ops/usage")).status == 404

    _run(_with_client(flow))


def test_data_endpoint_refuses_missing_and_wrong_tokens(env, monkeypatch):
    monkeypatch.setattr(Config, "OPS_TOKEN", "sesame")

    async def flow(client):
        assert (await client.get("/api/ops/usage")).status == 401
        assert (await client.get(
            "/api/ops/usage",
            headers={"Authorization": "Bearer wrong"})).status == 401
        # the page itself is inert html and serves without auth
        page = await client.get("/ops")
        assert page.status == 200
        assert "the meter" in await page.text()

    _run(_with_client(flow))


def test_report_shape_and_budget_column(env, monkeypatch):
    monkeypatch.setattr(Config, "OPS_TOKEN", "sesame")
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_SOFT_24H", 100)
    monkeypatch.setattr(Config, "USER_TOKEN_BUDGET_HARD_24H", 0)
    _log(env, U1, input_tokens=120, output_tokens=30, model="sonnet")
    _log(env, U1, input_tokens=10, hours_ago=30, role="scheduled",
         model="haiku")

    async def flow(client):
        res = await client.get(
            "/api/ops/usage",
            headers={"Authorization": "Bearer sesame"})
        assert res.status == 200
        report = await res.json()
        assert report["budgets"] == {"soft": 100, "hard": 0}
        assert {m["model"] for m in report["by_model"]} == {"sonnet", "haiku"}
        [user] = report["users"]
        assert user["name"] == "megan"
        assert user["input"] == 130
        # the 30h-old row is in the 14d window but NOT the 24h budget
        assert user["spent_24h"] == 150
        assert user["budget_state"] == "soft"

    _run(_with_client(flow))


def test_days_param_is_clamped_not_trusted(env, monkeypatch):
    monkeypatch.setattr(Config, "OPS_TOKEN", "sesame")

    async def flow(client):
        for bad in ("0", "9999", "not-a-number", "-3"):
            res = await client.get(
                f"/api/ops/usage?days={bad}",
                headers={"Authorization": "Bearer sesame"})
            assert res.status == 200
            assert 1 <= (await res.json())["days"] <= 90

    _run(_with_client(flow))
