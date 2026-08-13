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
import uuid as uuid_mod
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


def _send_body(text, client_message_id=None):
    return {"text": text,
            "client_message_id": client_message_id or str(uuid_mod.uuid4())}


class StubChatService:
    """the chat seam as the endpoint sees it: records the inbound message,
    delivers council lines through the app interface (the real router path),
    and returns None when delivery confirmed - or the fallback copy when the
    turn refused/errored."""

    def __init__(self, app_interface,
                 lines=(("pip", "one block!! just one."),),
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


def _seed_room_message(user, author_type, author, content):
    """write into the user's CURRENT room stream, like the engine would."""
    from src.managers.event_store_adapter import SqlEventStore
    from src.services.rooms import get_room_store
    from src.services.workspace.agenda import user_today
    from dainframe.core.events import NewEvent

    room = get_room_store().current_room(user, user_today(user))
    store = SqlEventStore(room["room_uuid"])
    _run(store.append(NewEvent(
        author_type=author_type, author=author, kind="message",
        content=content, platform="app")))
    return room


def test_room_messages_read_and_tenancy(env):
    # legacy history exists but lives in the archived legacy room, not today
    EventLog(U1).append_message("user", "user", "old life, old stream")
    _seed_room_message(U1, "user", "user", "hello from megan")
    _seed_room_message(U1, "agent", "pip", "hi!! ready when you are")
    _seed_room_message(U2, "user", "user", "someone else's life")

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
        # the legacy stream stays out of today's room
        assert all("old life" not in r["content"] for r in rows)
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
            json=_send_body("i want to start a chordial block"),
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert [m["author"] for m in body["messages"]] == ["pip", "mabel"]
        assert body["messages"][0]["content"] == "one block!! just one."

        # the turn saw an app-platform message for the device's user,
        # shaped as the COUNCIL's shared room (phase 3b): group scope with
        # the user as the fan-out key, so the routing spine picks a speaker
        unified = chat.seen[0]
        assert unified.platform == "app"
        assert unified.platform_user_id == U1
        assert unified.chat_scope == "group"
        assert unified.group_chat_id == U1

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
            "/api/v1/rooms/current/messages", json=_send_body("hi"),
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
                                 json=_send_body("hi"), headers=headers)
        assert resp.status == 503     # no chat service wired
    _run(_with_service(_service(), flow))

    app = AppInterface()
    chat = StubChatService(app)

    async def flow2(client):
        token = _token()
        headers = {"Authorization": f"Bearer {token}"}
        for bad in ({}, _send_body(""), _send_body("  "),
                    _send_body("x" * 4001), {"text": "hi"},
                    {"text": "hi", "client_message_id": "not-a-uuid"}):
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
            "type": "message", "id": payload["id"], "author": "skip",
            "content": "movement break?", "at": payload["at"],
        }
        assert payload["id"]  # stable per-line id for cross-surface dedup
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


# --- idempotent submission (the retry contract) -------------------------------


def test_retry_replays_without_a_second_turn(env):
    """a lost response + retry must never cost a second model turn."""
    app = AppInterface()
    chat = StubChatService(app, lines=[("pip", "on it!!")])

    async def flow(client):
        token = _token()
        headers = {"Authorization": f"Bearer {token}"}
        body = _send_body("start my block")
        first = await client.post("/api/v1/rooms/current/messages",
                                  json=body, headers=headers)
        assert first.status == 200
        first_body = await first.json()

        retry = await client.post("/api/v1/rooms/current/messages",
                                  json=body, headers=headers)
        assert retry.status == 200
        retry_body = await retry.json()
        assert retry_body["replayed"] is True
        assert retry_body["messages"] == first_body["messages"]
        assert len(chat.seen) == 1          # the model ran exactly once
    _run(_with_service(_service(chat, app), flow))


def test_fallback_turns_release_the_claim_for_retry(env):
    """error copy is never stored: a retry of an errored turn gets a fresh
    chance, not a replayed apology."""
    app = AppInterface()
    chat = StubChatService(app, fallback="having trouble right now")

    async def flow(client):
        token = _token()
        headers = {"Authorization": f"Bearer {token}"}
        body = _send_body("hi")
        await client.post("/api/v1/rooms/current/messages",
                          json=body, headers=headers)
        chat.fallback = None                 # the model recovers
        retry = await client.post("/api/v1/rooms/current/messages",
                                  json=body, headers=headers)
        retry_body = await retry.json()
        assert "replayed" not in retry_body
        assert retry_body["messages"][0]["author"] == "pip"
        assert len(chat.seen) == 2           # a real second attempt
    _run(_with_service(_service(chat, app), flow))


def test_concurrent_sends_do_not_cross_contaminate(env):
    """two in-flight POSTs must each get their own turn's lines - the
    per-user send lock serializes them end to end."""
    app = AppInterface()

    class EchoChat(StubChatService):
        async def process_message(self, unified):
            self.seen.append(unified)
            await asyncio.sleep(0.05)   # let the second POST pile up
            await self.app_interface.send_message(
                unified.platform_user_id, f"re: {unified.content}",
                speaker="pip")
            return None

    chat = EchoChat(app)

    async def flow(client):
        token = _token()
        headers = {"Authorization": f"Bearer {token}"}

        async def send(text):
            resp = await client.post("/api/v1/rooms/current/messages",
                                     json=_send_body(text), headers=headers)
            return await resp.json()

        a, b = await asyncio.gather(send("alpha"), send("beta"))
        assert [m["content"] for m in a["messages"]] == ["re: alpha"]
        assert [m["content"] for m in b["messages"]] == ["re: beta"]
    _run(_with_service(_service(chat, app), flow))


# --- limit clamping -----------------------------------------------------------


def test_negative_limit_is_clamped(env):
    _seed_room_message(U1, "user", "user", "only row")

    async def flow(client):
        token = _token()
        headers = {"Authorization": f"Bearer {token}"}
        for bad_limit in ("-1", "0", "99999"):
            resp = await client.get(
                f"/api/v1/rooms/current/messages?limit={bad_limit}",
                headers=headers)
            assert resp.status == 200, bad_limit
            assert len((await resp.json())["messages"]) == 1
    _run(_with_service(_service(), flow))


# --- tasks over the device api (the deer window's canonical seam) --------------


def test_task_create_finish_and_done_bucket(env):
    async def flow(client):
        token = _token(U1)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.post("/api/v1/tasks",
                                 json={"title": "bounce stems"},
                                 headers=headers)
        assert resp.status == 201
        task = (await resp.json())["task"]
        assert task["title"] == "bounce stems"

        today = await client.get("/api/v1/today", headers=headers)
        buckets = (await today.json())["buckets"]
        assert [t["title"] for t in buckets["today"]] == ["bounce stems"]
        assert buckets["done"] == []

        resp = await client.post(f"/api/v1/tasks/{task['id']}/status",
                                 json={"status": "done"}, headers=headers)
        assert resp.status == 200
        assert (await resp.json())["task"]["status"] == "done"

        today = await client.get("/api/v1/today", headers=headers)
        buckets = (await today.json())["buckets"]
        assert buckets["today"] == []
        assert [t["title"] for t in buckets["done"]] == ["bounce stems"]
        assert buckets["done"][0]["closed_at"]
    _run(_with_service(_service(), flow))


def test_task_endpoints_validate_and_scope(env):
    async def flow(client):
        token = _token(U1)
        headers = {"Authorization": f"Bearer {token}"}
        for bad in ({}, {"title": ""}, {"title": "   "},
                    {"title": "x" * 301}):
            resp = await client.post("/api/v1/tasks", json=bad,
                                     headers=headers)
            assert resp.status == 400, bad

        resp = await client.post("/api/v1/tasks", json={"title": "mine"},
                                 headers=headers)
        task = (await resp.json())["task"]

        # another tenant can't touch it
        other = {"Authorization": f"Bearer {_token(U2)}"}
        resp = await client.post(f"/api/v1/tasks/{task['id']}/status",
                                 json={"status": "done"}, headers=other)
        assert resp.status == 404

        resp = await client.post(f"/api/v1/tasks/{task['id']}/status",
                                 json={"status": "not-a-status"},
                                 headers=headers)
        assert resp.status == 400

        resp = await client.post("/api/v1/tasks/abc/status",
                                 json={"status": "done"}, headers=headers)
        assert resp.status == 400
    _run(_with_service(_service(), flow))


# --- the council roster (the presence strip's data source) ---------------------


def test_council_roster_authored_defaults(env, monkeypatch):
    from config import Config
    monkeypatch.setattr(
        Config, "ENABLED_HELPERS",
        ["vel", "pip", "skip", "remy", "mabel", "juniper", "edwin"])

    async def flow(client):
        token = _token()
        resp = await client.get(
            "/api/v1/council", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        members = (await resp.json())["council"]
        assert [m["id"] for m in members] == \
            ["vel", "edwin", "juniper", "mabel", "pip", "remy", "skip"]

        vel = members[0]
        # the chair leads the strip and never reads as unmet - she is the
        # front door and greets before any introduction is recorded
        assert vel["chair"] is True
        assert vel["status"] == "active"
        assert vel["emoji"] == "🦌"
        assert vel["species"] == "deer"
        assert vel["name"] == "vel"

        for member in members[1:]:
            assert member["chair"] is False
            assert member["status"] == "not_met"
            assert member["name"] == member["id"]
    _run(_with_service(_service(), flow))


def test_council_roster_overrides_and_partial_deployment(env, monkeypatch):
    from config import Config
    from src.database.models import HelperState
    monkeypatch.setattr(Config, "ENABLED_HELPERS", ["vel", "pip"])
    with db_mod.SessionLocal() as s:
        # megan reshaped pip; a different tenant's reshape must not leak
        s.add(HelperState(user_uuid=U1, helper_id="pip", status="active",
                          persona_name="sprocket", persona_form="tiny robot"))
        s.add(HelperState(user_uuid=U2, helper_id="pip", status="active",
                          persona_name="intruder"))
        s.commit()

    async def flow(client):
        token = _token(U1)
        resp = await client.get(
            "/api/v1/council", headers={"Authorization": f"Bearer {token}"})
        members = (await resp.json())["council"]
        # only the deployed residents - no ghost councilors from other cards
        assert [m["id"] for m in members] == ["vel", "pip"]
        pip = members[1]
        assert pip["name"] == "sprocket"
        assert pip["species"] == "tiny robot"
        assert pip["status"] == "active"
        # authored facts still ride along for the ui
        assert pip["emoji"] == "🐿️"
        assert pip["lane"] == "productivity"
    _run(_with_service(_service(), flow))


def test_council_requires_device_auth(env):
    async def flow(client):
        resp = await client.get("/api/v1/council")
        assert resp.status == 401
    _run(_with_service(_service(), flow))


# --- websocket revocation -----------------------------------------------------


def test_revoked_device_socket_closes_within_the_window(env, monkeypatch):
    import src.web.server as server_mod
    monkeypatch.setattr(server_mod, "WS_REVALIDATE_SECONDS", 0.1)
    app = AppInterface()

    async def flow(client):
        code = device_auth.mint_device_link_code(U1)
        device_uuid, token = device_auth.link_device(code, "revocable")
        ws = await client.ws_connect("/api/v1/ws")
        await ws.send_json({"token": token})
        assert (await ws.receive_json())["type"] == "hello"

        device_auth.revoke_device(U1, device_uuid)
        msg = await asyncio.wait_for(ws.receive(), timeout=5)
        assert msg.type.name in ("CLOSE", "CLOSING", "CLOSED")
        assert ws.close_code == 4401
        # and its queue is gone - the council can't be overheard
        await asyncio.sleep(0.05)
        assert await app.send_message(U1, "still there?") is False
    _run(_with_service(_service(None, app), flow))


# --- the archive (phase 5b: archives are forever) ------------------------------


def test_archive_lists_days_with_summaries(env):
    room = _seed_room_message(U1, "user", "user", "yesterday's words")
    _seed_room_message(U1, "agent", "vel", "held gently")
    from src.services.rooms import get_room_store
    get_room_store().close_room(room["id"])

    async def flow(client):
        headers = {"Authorization": f"Bearer {_token(U1)}"}
        resp = await client.get("/api/v1/rooms", headers=headers)
        assert resp.status == 200
        rooms = (await resp.json())["rooms"]
        closed = [r for r in rooms if r["status"] == "closed"]
        assert len(closed) == 1
        assert closed[0]["room_uuid"] == room["room_uuid"]
        assert "1 user messages" in closed[0]["summary"]
        assert "user_uuid" not in closed[0]

        # an unauthenticated caller sees nothing
        resp = await client.get("/api/v1/rooms")
        assert resp.status == 401
    _run(_with_service(_service(), flow))


def test_archived_transcript_reads_and_tenancy(env):
    room = _seed_room_message(U1, "user", "user", "a remembered day")
    from src.services.rooms import get_room_store
    get_room_store().close_room(room["id"])

    async def flow(client):
        headers = {"Authorization": f"Bearer {_token(U1)}"}
        resp = await client.get(
            f"/api/v1/rooms/{room['room_uuid']}/messages", headers=headers)
        assert resp.status == 200
        rows = (await resp.json())["messages"]
        assert [r["content"] for r in rows] == ["a remembered day"]

        # someone else's archive answers exactly like a room that never was
        other = {"Authorization": f"Bearer {_token(U2)}"}
        resp = await client.get(
            f"/api/v1/rooms/{room['room_uuid']}/messages", headers=other)
        assert resp.status == 404
        resp = await client.get("/api/v1/rooms/not-a-room/messages",
                                headers=headers)
        assert resp.status == 404

        # the /current route still resolves as itself, never as a uuid
        resp = await client.get("/api/v1/rooms/current/messages",
                                headers=headers)
        assert resp.status == 200
    _run(_with_service(_service(), flow))
