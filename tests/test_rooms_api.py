"""rooms api v0 + the app delivery interface (phase 1b of the rooms overhaul).

the legacy per-user stream presented as today's room: read messages, send a
message through the real chat seam, and receive council lines over the
websocket. the AppInterface is a router-registered platform whose delivery
lands in per-user subscriber queues - confirmed only when a surface was
actually listening, so the engine's confirmed-send-before-recording invariant
keeps meaning something for the app.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, PlatformIdentity, User
from src.managers.event_log import EventLog
from src.managers.user_manager import UserManager
from src.providers.platforms.app import AppInterface
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"
U2 = "user-two"


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
        s.add(User(uuid=U2, preferred_name="guest", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


def _token(user=U1):
    code = device_auth.mint_device_link_code(user)
    result = device_auth.link_device(code, "test-device")
    assert result is not None
    return result[1]


def _run(coro):
    return asyncio.run(coro)


class StubChatService:
    """the chat seam as the endpoint sees it: records the inbound message,
    delivers council lines through the app interface (the real router path),
    and returns None when delivery confirmed - or the fallback copy when the
    turn refused/errored."""

    def __init__(self, app_interface, lines=(("pip", "one block!! just one.")),
                 fallback=None):
        self.user_manager = UserManager()
        self.app_interface = app_interface
        self.lines = lines
        self.fallback = fallback
        self.seen = []

    async def process_message(self, unified):
        self.seen.append(unified)
        if self.fallback is not None:
            return self.fallback
        log = EventLog(unified.platform_user_id)
        await asyncio.to_thread(
            log.append_message, "user", "user", unified.content,
            platform="app")
        delivered = False
        for speaker, text in self.lines:
            ok = await self.app_interface.send_message(
                unified.platform_user_id, text, speaker=speaker)
            if ok:
                await asyncio.to_thread(
                    log.append_message, "agent", speaker, text,
                    platform="app")
                delivered = True
        return None if delivered else "error copy"


async def _with_service(service, fn):
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def _service(chat_service=None, app_interface=None):
    return WebService(user_resolver=lambda: U1, chat_service=chat_service,
                      app_interface=app_interface)


# --- the app interface --------------------------------------------------------


def test_app_interface_confirms_only_with_a_listener(env):
    async def flow():
        app = AppInterface()
        assert await app.send_message(U1, "into the void") is False

        queue = app.subscribe(U1)
        assert await app.send_message(U1, "hello", speaker="mabel") is True
        payload = queue.get_nowait()
        assert payload["author"] == "mabel"
        assert payload["content"] == "hello"
        app.unsubscribe(U1, queue)
        assert await app.send_message(U1, "gone again") is False
    _run(flow())


def test_app_interface_fans_out_to_every_surface(env):
    async def flow():
        app = AppInterface()
        q1, q2 = app.subscribe(U1), app.subscribe(U1)
        other = app.subscribe(U2)
        assert await app.send_message(U1, "hi") is True
        assert q1.get_nowait()["content"] == "hi"
        assert q2.get_nowait()["content"] == "hi"
        assert other.empty()          # tenant isolation at the fan-out
    _run(flow())


# --- room read endpoints ------------------------------------------------------


def test_room_current_descriptor(env):
    async def flow(client):
        token = _token()
        resp = await client.get(
            "/api/v1/rooms/current",
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert body["room"]["type"] == "daily"
        assert body["chat_available"] is False   # no chat seam wired here
    _run(_with_service(_service(), flow))


def test_room_messages_read_and_tenancy(env):
    log1, log2 = EventLog(U1), EventLog(U2)
    log1.append_message("user", "user", "hello from megan", platform="app")
    log1.append_message("agent", "pip", "hi!! ready when you are",
                        platform="app")
    log1.append_action("pip", "start_focus", {"task": 1}, "started")
    log2.append_message("user", "user", "someone else's life")

    async def flow(client):
        token = _token(U1)
        resp = await client.get(
            "/api/v1/rooms/current/messages",
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        rows = (await resp.json())["messages"]
        assert [r["content"] for r in rows] == \
            ["hello from megan", "hi!! ready when you are"]
        assert rows[1]["author"] == "pip"
        assert rows[1]["author_type"] == "agent"
        # actions don't render in the app's message list (v0)
        assert all("start_focus" not in r["content"] for r in rows)
    _run(_with_service(_service(), flow))


# --- sending ------------------------------------------------------------------


def test_send_message_round_trip(env):
    app = AppInterface()
    chat = StubChatService(app, lines=[("pip", "one block!! just one."),
                                       ("mabel", "and water first.")])

    async def flow(client):
        token = _token()
        resp = await client.post(
            "/api/v1/rooms/current/messages",
            json={"text": "i want to start a chordial block"},
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert [m["author"] for m in body["messages"]] == ["pip", "mabel"]
        assert body["messages"][0]["content"] == "one block!! just one."

        # the turn saw an app-platform message for the device's user
        unified = chat.seen[0]
        assert unified.platform == "app"
        assert unified.platform_user_id == U1

        # the app platform identity got bound to the right user
        with db_mod.SessionLocal() as s:
            row = s.query(PlatformIdentity).filter(
                PlatformIdentity.platform == "app").one()
            assert row.user_uuid == U1
    _run(_with_service(_service(chat, app), flow))


def test_send_fallback_copy_is_returned_not_persisted(env):
    app = AppInterface()
    chat = StubChatService(app, fallback="i don't think i can help with that")

    async def flow(client):
        token = _token()
        resp = await client.post(
            "/api/v1/rooms/current/messages", json={"text": "hi"},
            headers={"Authorization": f"Bearer {token}"})
        body = await resp.json()
        assert body["messages"][0]["ephemeral"] is True
        assert "can't" in body["messages"][0]["content"] or \
            "can help" in body["messages"][0]["content"]
        # nothing landed in history
        assert EventLog(U1).recent() == []
    _run(_with_service(_service(chat, app), flow))


def test_send_requires_chat_seam_and_valid_body(env):
    async def flow(client):
        token = _token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post("/api/v1/rooms/current/messages",
                                 json={"text": "hi"}, headers=headers)
        assert resp.status == 503     # no chat service wired
    _run(_with_service(_service(), flow))

    app = AppInterface()
    chat = StubChatService(app)

    async def flow2(client):
        token = _token()
        headers = {"Authorization": f"Bearer {token}"}
        for bad in ({}, {"text": ""}, {"text": "  "}, {"text": "x" * 4001}):
            resp = await client.post("/api/v1/rooms/current/messages",
                                     json=bad, headers=headers)
            assert resp.status == 400, bad
        assert chat.seen == []
    _run(_with_service(_service(chat, app), flow2))


# --- the websocket ------------------------------------------------------------


def test_ws_auth_then_receives_council_lines(env):
    app = AppInterface()

    async def flow(client):
        token = _token()
        ws = await client.ws_connect("/api/v1/ws")
        await ws.send_json({"token": token})
        hello = await ws.receive_json()
        assert hello["type"] == "hello"

        assert await app.send_message(U1, "movement break?",
                                      speaker="skip") is True
        payload = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert payload == {
            "type": "message", "author": "skip",
            "content": "movement break?", "at": payload["at"],
        }
        await ws.close()
        # the socket's queue is gone once the connection closes
        await asyncio.sleep(0)
        assert await app.send_message(U1, "anyone?") is False
    _run(_with_service(_service(None, app), flow))


def test_ws_rejects_bad_token(env):
    app = AppInterface()

    async def flow(client):
        ws = await client.ws_connect("/api/v1/ws")
        await ws.send_json({"token": "garbage"})
        msg = await ws.receive()
        assert msg.type.name in ("CLOSE", "CLOSING", "CLOSED")
        assert ws.close_code == 4401
    _run(_with_service(_service(None, app), flow))
