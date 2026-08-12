"""the sidecar: local store, focus blocks, authored lines, and the outbox
pump's round trip into the real sync contract (phase 4b of the rooms
overhaul - the deer's machinery).

the clock is injected everywhere: tests move time, they don't sleep
through it.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, DeviceEvent, User
from src.sidecar.lines import LINE_POOLS, pick_line
from src.sidecar.sessions import SessionEngine, SessionError
from src.sidecar.server import SidecarService
from src.sidecar.store import SidecarStore
from src.sidecar.sync import OutboxPump
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path):
    s = SidecarStore(tmp_path / "sidecar.db")
    yield s
    s.close()


class Clock:
    """a hand-cranked clock."""

    def __init__(self):
        self.now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


# --- the store ------------------------------------------------------------


def test_outbox_seq_is_monotonic_across_trims(store):
    store.enqueue("focus_block.started", {"n": 1})
    store.enqueue("focus_block.completed", {"n": 2})
    store.trim(2)
    assert store.pending() == []
    seq = store.enqueue("focus_block.started", {"n": 3})
    # AUTOINCREMENT never reuses a seq - the server's (device, seq)
    # uniqueness depends on it
    assert seq == 3


def test_outbox_rebase_renumbers_from_one(store):
    store.enqueue("focus_block.started", {"n": 1})
    store.enqueue("focus_block.completed", {"n": 2})
    store.trim(1)                       # first event acked under old device
    moved = store.rebase_outbox()
    assert moved == 1
    pending = store.pending()
    assert [e["seq"] for e in pending] == [1]
    assert pending[0]["payload"] == {"n": 2}
    # and the sequence really restarts - the next event is seq 2, not 4
    assert store.enqueue("focus_block.started", {"n": 3}) == 2


def test_kv_roundtrip(store):
    assert store.get("device_token") is None
    store.put("device_token", "abc.def")
    store.put("device_token", "abc.xyz")
    assert store.get("device_token") == "abc.xyz"


# --- the session engine ---------------------------------------------------


def test_block_lifecycle_completed_by_tick(store):
    clock = Clock()
    engine = SessionEngine(store, clock=clock)
    state = engine.start("write the novel", minutes=25)
    assert state["active"] is True
    assert state["remaining_seconds"] == 25 * 60

    assert engine.tick() is None               # mid-block: nothing settles
    clock.advance(minutes=25)
    assert engine.tick() == "completed"
    assert engine.state() == {"active": False}

    events = store.pending()
    assert [e["type"] for e in events] == \
        ["focus_block.started", "focus_block.completed"]
    done = events[1]
    assert done["payload"]["label"] == "write the novel"
    assert done["payload"]["elapsed_seconds"] == 25 * 60
    # the completion is stamped at the clock's end, not whenever tick ran
    assert done["occurred_at"] == state["ends_at"]


def test_stop_early_is_abandoned_stop_late_is_completed(store):
    clock = Clock()
    engine = SessionEngine(store, clock=clock)
    engine.start(minutes=25)
    clock.advance(minutes=10)
    assert engine.stop() == "abandoned"
    abandoned = store.pending()[-1]
    assert abandoned["type"] == "focus_block.abandoned"
    assert abandoned["payload"]["elapsed_seconds"] == 10 * 60

    engine.start(minutes=25)
    clock.advance(minutes=26)                 # the ding went unacknowledged
    assert engine.stop() == "completed"
    assert engine.stop() is None              # nothing left running


def test_start_guards(store):
    engine = SessionEngine(store, clock=Clock())
    engine.start(minutes=25)
    with pytest.raises(SessionError):
        engine.start(minutes=25)              # one block at a time
    for bad in (0, -5, 500, "25", True):
        with pytest.raises(SessionError):
            SessionEngine(store, clock=Clock()).start(minutes=bad)


def test_block_survives_a_sidecar_restart(store):
    clock = Clock()
    SessionEngine(store, clock=clock).start("resumable", minutes=25)
    clock.advance(minutes=5)
    # a fresh engine over the same store IS the restarted sidecar
    engine = SessionEngine(store, clock=clock)
    assert engine.state()["remaining_seconds"] == 20 * 60
    clock.advance(minutes=21)                 # expired while "down"
    assert engine.tick() == "completed"


# --- lines -------------------------------------------------------------------


def test_lines_never_repeat_and_unknown_moments_are_silent():
    last = pick_line("session_start")
    for _ in range(50):
        line = pick_line("session_start", last=last)
        assert line != last
        assert line in LINE_POOLS["session_start"]
        last = line
    assert pick_line("no_such_moment") == ""


# --- the outbox pump: a real round trip into the sync contract ---------------


@pytest.fixture()
def server_env(monkeypatch):
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


def test_pump_round_trip_acks_and_trims(server_env, store):
    async def flow():
        web_service = WebService(user_resolver=lambda: U1)
        client = TestClient(TestServer(web_service.build_app()))
        await client.start_server()
        try:
            code = device_auth.mint_device_link_code(U1)
            _device_uuid, token = device_auth.link_device(code, "deer-test")
            store.put("device_token", token)
            store.put("server_url", str(client.make_url("")).rstrip("/"))

            store.enqueue("focus_block.completed",
                          {"label": "novel", "minutes": 25,
                           "elapsed_seconds": 1500})
            store.enqueue("not.a.real.type", {})   # tombstones, never pins

            pump = OutboxPump(store)
            try:
                result = await pump.push_once()
            finally:
                await pump.close()

            assert result["acked_seq"] == 2
            assert result["applied"] == 1
            assert len(result["rejected"]) == 1
            assert store.pending() == []           # trimmed at the cursor

            with db_mod.SessionLocal() as s:
                rows = s.query(DeviceEvent).order_by(DeviceEvent.seq).all()
                assert [r.rejected for r in rows] == [False, True]
                assert rows[0].event_type == "focus_block.completed"
                assert rows[0].payload["label"] == "novel"
        finally:
            await client.close()
    _run(flow())


def test_pump_parks_on_revoked_device(server_env, store):
    async def flow():
        web_service = WebService(user_resolver=lambda: U1)
        client = TestClient(TestServer(web_service.build_app()))
        await client.start_server()
        try:
            code = device_auth.mint_device_link_code(U1)
            device_uuid, token = device_auth.link_device(code, "revoked")
            device_auth.revoke_device(U1, device_uuid)
            store.put("device_token", token)
            store.put("server_url", str(client.make_url("")).rstrip("/"))
            store.enqueue("focus_block.completed", {})

            pump = OutboxPump(store)
            try:
                assert await pump.push_once() is None
            finally:
                await pump.close()
            assert store.get("sync_error")
            assert store.pending() != []           # nothing lost, just parked
        finally:
            await client.close()
    _run(flow())


# --- the sidecar's own api -----------------------------------------------------


def _sidecar_client_flow(store, clock, fn):
    async def flow():
        service = SidecarService(store,
                                 engine=SessionEngine(store, clock=clock))
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            await fn(client, service)
        finally:
            await client.close()
    _run(flow())


def test_sidecar_session_flow_and_ws_push(store):
    clock = Clock()

    async def flow(client, _service):
        ws = await client.ws_connect("/v1/ws")
        hello = await ws.receive_json()
        assert hello == {"type": "state", "session": {"active": False}}

        resp = await client.post("/v1/session/start",
                                 json={"label": "novel", "minutes": 25})
        assert resp.status == 200
        body = await resp.json()
        assert body["session"]["active"] is True
        assert body["line"] in LINE_POOLS["session_start"]

        line = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert line["type"] == "line" and line["moment"] == "session_start"
        state = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert state["session"]["active"] is True

        dup = await client.post("/v1/session/start", json={})
        assert dup.status == 400                   # one block at a time

        clock.advance(minutes=5)
        resp = await client.post("/v1/session/stop")
        body = await resp.json()
        assert body["outcome"] == "abandoned"
        assert body["line"] in LINE_POOLS["session_abandoned"]

        idle = await client.post("/v1/session/stop")
        assert idle.status == 409
        await ws.close()
    _sidecar_client_flow(store, clock, flow)


def test_sidecar_handshake_rebases_on_new_device(store):
    clock = Clock()
    store.enqueue("focus_block.completed", {"n": 1})
    store.trim(1)
    store.enqueue("focus_block.completed", {"n": 2})   # pending at seq 2

    async def flow(client, _service):
        bad = await client.post("/v1/handshake", json={"token": "nodot"})
        assert bad.status == 400

        resp = await client.post("/v1/handshake", json={
            "token": "device-a.secret", "server_url": "http://localhost:1"})
        assert resp.status == 200
        assert store.pending()[0]["seq"] == 2          # same device: untouched

        store.put("sync_error", "device token rejected (401)")
        resp = await client.post("/v1/handshake", json={
            "token": "device-b.secret", "server_url": "http://localhost:1"})
        assert resp.status == 200
        assert store.pending()[0]["seq"] == 1          # new device: rebased
        assert not store.get("sync_error")             # pump un-parked
    _sidecar_client_flow(store, clock, flow)


def test_sidecar_cors_refuses_strangers(store):
    clock = Clock()

    async def flow(client, _service):
        evil = await client.get("/v1/state",
                                headers={"Origin": "https://evil.example"})
        assert evil.status == 403
        ok = await client.get("/v1/state",
                              headers={"Origin": "http://localhost:1420"})
        assert ok.status == 200
        assert ok.headers["Access-Control-Allow-Origin"] == \
            "http://localhost:1420"
        # no Origin header = not a browser = the tauri shell / curl
        bare = await client.get("/v1/state")
        assert bare.status == 200
    _sidecar_client_flow(store, clock, flow)
