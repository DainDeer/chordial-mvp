"""device identity + the sync contract (phase 1 of the rooms overhaul).

covers the two ROOMS_DESIGN.md section-10 guarantees end to end:
- devices are individually revocable credentials, and every /api/v1 request
  is tenant-scoped to the authenticated device's user
- device events apply exactly once no matter how often the outbox retries:
  unique event_uuid + unique (device, seq), durable contiguous ACK cursor,
  rejected events never block the batch, gaps hold the cursor until filled
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
from config import Config
from src.database.models import Base, Device, DeviceEvent, User
from src.services import device_sync
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


def _link(user=U1, name="test-device"):
    code = device_auth.mint_device_link_code(user)
    result = device_auth.link_device(code, name)
    assert result is not None
    return result  # (device_uuid, token)


def _event(seq, event_type="focus_block.completed", **payload):
    return {
        "id": str(uuid_mod.uuid4()),
        "seq": seq,
        "type": event_type,
        "payload": payload or {"minutes": 25},
    }


def _device_pk(env, device_uuid):
    with env() as s:
        return s.query(Device.id).filter(
            Device.device_uuid == device_uuid).scalar()


# --- linking & tokens ---------------------------------------------------------


def test_link_device_mints_working_token(env):
    device_uuid, token = _link()
    identity = device_auth.verify_device_token(token)
    assert identity is not None
    assert identity.user_uuid == U1
    assert identity.device_uuid == device_uuid


def test_link_code_is_single_use(env):
    code = device_auth.mint_device_link_code(U1)
    assert device_auth.link_device(code) is not None
    assert device_auth.link_device(code) is None


def test_web_login_code_cannot_link_device(env):
    from src.web import auth as web_auth
    code = web_auth.mint_login_code(U1)
    assert device_auth.link_device(code) is None


def test_secret_is_stored_hashed_and_wrong_secret_fails(env):
    device_uuid, token = _link()
    with env() as s:
        stored = s.query(Device.token_hash).filter(
            Device.device_uuid == device_uuid).scalar()
    assert token.partition(".")[2] not in stored
    assert device_auth.verify_device_token(f"{device_uuid}.wrong") is None
    assert device_auth.verify_device_token("garbage") is None
    assert device_auth.verify_device_token(None) is None


def test_revocation_fails_closed_and_is_scoped(env):
    device_uuid, token = _link()
    # another user cannot revoke it
    assert device_auth.revoke_device(U2, device_uuid) is False
    assert device_auth.verify_device_token(token) is not None
    # the owner can; the token dies; revoking again is a no-op
    assert device_auth.revoke_device(U1, device_uuid) is True
    assert device_auth.verify_device_token(token) is None
    assert device_auth.revoke_device(U1, device_uuid) is False


# --- the sync contract --------------------------------------------------------


def test_batch_applies_and_acks_contiguously(env):
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    result = device_sync.apply_events(pk, [_event(1), _event(2), _event(3)])
    assert result.acked_seq == 3
    assert result.applied == 3
    assert result.duplicates == 0
    assert result.rejected == []


def test_retry_is_a_noop_that_still_acks(env):
    """at-least-once delivery, exactly-once apply: the whole batch again."""
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    batch = [_event(1), _event(2)]
    device_sync.apply_events(pk, batch)
    result = device_sync.apply_events(pk, batch)
    assert result.acked_seq == 2
    assert result.applied == 0
    assert result.duplicates == 2
    with env() as s:
        assert s.query(DeviceEvent).count() == 2  # no double rows


def test_same_seq_different_uuid_is_still_a_duplicate(env):
    """(device, seq) is unique on its own - a client bug that re-mints ids
    for already-sent seqs must not double-apply."""
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    device_sync.apply_events(pk, [_event(1)])
    result = device_sync.apply_events(pk, [_event(1)])
    assert result.duplicates == 1
    assert result.acked_seq == 1


def test_gap_holds_the_cursor_until_filled(env):
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    result = device_sync.apply_events(pk, [_event(1), _event(2), _event(4)])
    assert result.acked_seq == 2          # 4 landed but is not contiguous
    assert result.applied == 3
    result = device_sync.apply_events(pk, [_event(3)])
    assert result.acked_seq == 4          # gap filled; cursor jumps past 4


def test_rejected_events_never_block_the_batch(env):
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    bad_type = _event(2, event_type="totally.made.up")
    bad_seq = {"id": str(uuid_mod.uuid4()), "seq": "two",
               "type": "focus_block.completed", "payload": {}}
    bad_id = {"id": "not-a-uuid", "seq": 3,
              "type": "focus_block.completed", "payload": {}}
    result = device_sync.apply_events(
        pk, [_event(1), bad_type, bad_seq, bad_id, "not even a dict"])
    assert result.applied == 1
    # the semantically-bad event tombstones and CONSUMES seq 2 - the cursor
    # passes it instead of pinning at 1 forever (structurally-unparseable
    # entries have no seq to consume, so 3 stays open for the client to fix)
    assert result.acked_seq == 2
    assert len(result.rejected) == 4
    errors = " ".join(str(r["error"]) for r in result.rejected)
    assert "unknown event type" in errors


def test_tombstone_consumes_its_seq_and_dedupes_on_retry(env):
    """the P1: a rejected event must never pin the ACK cursor. seq 2 is
    semantically invalid; 3 lands after it; the cursor still reaches 3, and
    retrying the batch is pure duplicates."""
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    batch = [_event(1), _event(2, event_type="nope.nope"), _event(3)]
    result = device_sync.apply_events(pk, batch)
    assert result.acked_seq == 3
    assert result.applied == 2          # tombstones aren't "applied" facts
    assert [r["error"] for r in result.rejected] == \
        ["unknown event type: 'nope.nope'"]

    retry = device_sync.apply_events(pk, batch)
    assert retry.acked_seq == 3
    assert retry.applied == 0
    assert retry.duplicates == 3        # tombstone dedupes like any row
    assert retry.rejected == []

    with env() as s:
        row = s.query(DeviceEvent).filter(DeviceEvent.seq == 2).one()
        assert row.rejected is True
        assert "unknown event type" in row.error
        clean = s.query(DeviceEvent).filter(DeviceEvent.seq == 3).one()
        assert clean.rejected is False


def test_cross_device_uuid_duplicate_consumes_the_new_seq(env):
    """the relink hazard: an event acked under the OLD device whose response
    was lost gets re-sent by the rebased outbox under the NEW device at
    seq 1. the fact must not apply twice - but if the new seq isn't
    consumed, the new cursor pins at zero forever and every later event is
    stuck behind it."""
    d_old, _ = _link(name="old-life")
    d_new, _ = _link(name="new-life")
    old_pk, new_pk = _device_pk(env, d_old), _device_pk(env, d_new)

    event = _event(1, minutes=25)
    assert device_sync.apply_events(old_pk, [event]).acked_seq == 1

    # the rebased outbox re-sends the same uuid as the new device's seq 1,
    # with a genuinely new event behind it
    replay = dict(event, seq=1)
    fresh = _event(2, minutes=10)
    result = device_sync.apply_events(new_pk, [replay, fresh])

    assert result.acked_seq == 2          # NOT pinned at zero
    assert result.applied == 1            # only the genuinely new fact
    assert result.duplicates == 1

    with env() as s:
        rows = s.query(DeviceEvent).filter(
            DeviceEvent.device_id == new_pk).order_by(DeviceEvent.seq).all()
        assert [r.seq for r in rows] == [1, 2]
        tombstone = rows[0]
        assert tombstone.rejected is True
        assert tombstone.event_type == "duplicate"
        assert tombstone.payload["original_event_uuid"] == event["id"]
        # the original fact exists exactly once, under the old device
        facts = s.query(DeviceEvent).filter(
            DeviceEvent.event_uuid == event["id"]).all()
        assert len(facts) == 1 and facts[0].device_id == old_pk

    # and the retry of that batch is a pure no-op that still acks
    again = device_sync.apply_events(new_pk, [replay, fresh])
    assert again.acked_seq == 2
    assert again.applied == 0
    assert again.duplicates == 2


def test_batch_size_and_daily_quota_guard(env, monkeypatch):
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    monkeypatch.setattr(Config, "SYNC_MAX_BATCH", 2)
    with pytest.raises(ValueError):
        device_sync.apply_events(pk, [_event(1), _event(2), _event(3)])
    monkeypatch.setattr(Config, "SYNC_MAX_BATCH", 500)
    monkeypatch.setattr(Config, "SYNC_DAILY_EVENT_CAP", 3)
    batch = [_event(1), _event(2), _event(3)]
    device_sync.apply_events(pk, batch)
    with pytest.raises(device_sync.QuotaExceeded):
        device_sync.apply_events(pk, [_event(4), _event(5)])


def test_retry_at_the_cap_still_returns_the_ack(env, monkeypatch):
    """the P1: quota counts only newly inserted rows. a user at the cap
    retrying already-applied events gets the durable cursor back - a 429
    there would deadlock dropped-response recovery."""
    device_uuid, _ = _link()
    pk = _device_pk(env, device_uuid)
    monkeypatch.setattr(Config, "SYNC_DAILY_EVENT_CAP", 2)
    batch = [_event(1), _event(2)]
    device_sync.apply_events(pk, batch)
    result = device_sync.apply_events(pk, batch)   # retry, at the cap
    assert result.acked_seq == 2
    assert result.duplicates == 2


def test_events_are_tenant_stamped(env):
    device_uuid, _ = _link(U1)
    pk = _device_pk(env, device_uuid)
    device_sync.apply_events(pk, [_event(1)])
    with env() as s:
        row = s.query(DeviceEvent).one()
        assert row.user_uuid == U1


# --- the /api/v1 endpoints ----------------------------------------------------
# same async harness as test_web_auth: sync tests drive an async flow via
# asyncio.run - no async pytest plugin in the suite, and none needed.


def _run(coro):
    return asyncio.run(coro)


async def _with_client(fn):
    service = WebService(user_resolver=lambda: U1)
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def test_link_endpoint_and_bad_code(env):
    async def flow(client):
        code = device_auth.mint_device_link_code(U1)
        resp = await client.post("/api/v1/devices/link",
                                 json={"code": code, "name": "megans-mac"})
        assert resp.status == 201
        body = await resp.json()
        assert body["token"].startswith(body["device_id"] + ".")

        resp = await client.post("/api/v1/devices/link",
                                 json={"code": "NOPE1234"})
        assert resp.status == 401
    _run(_with_client(flow))


def test_v1_requires_bearer_auth(env):
    async def flow(client):
        for route in ("/api/v1/devices", "/api/v1/today"):
            resp = await client.get(route)
            assert resp.status == 401, route
        resp = await client.post("/api/v1/sync/events", json={"events": []})
        assert resp.status == 401
    _run(_with_client(flow))


def test_sync_endpoint_round_trip(env):
    async def flow(client):
        _, token = _link()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/sync/events",
            json={"events": [_event(1), _event(2)]}, headers=headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["acked_seq"] == 2 and body["applied"] == 2

        resp = await client.post("/api/v1/sync/events",
                                 json={"events": "nope"}, headers=headers)
        assert resp.status == 400
    _run(_with_client(flow))


def test_quota_maps_to_429_at_the_http_layer(env, monkeypatch):
    monkeypatch.setattr(Config, "SYNC_DAILY_EVENT_CAP", 1)

    async def flow(client):
        _, token = _link()
        resp = await client.post(
            "/api/v1/sync/events",
            json={"events": [_event(1), _event(2)]},
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 429
    _run(_with_client(flow))


def test_device_list_is_tenant_scoped(env):
    async def flow(client):
        _, token1 = _link(U1, name="megans-mac")
        _, token2 = _link(U2, name="guests-laptop")
        resp = await client.get(
            "/api/v1/devices", headers={"Authorization": f"Bearer {token1}"})
        body = await resp.json()
        assert {d["name"] for d in body["devices"]} == {"megans-mac"}
        assert [d for d in body["devices"] if d["current"]]

        resp = await client.get(
            "/api/v1/devices", headers={"Authorization": f"Bearer {token2}"})
        body = await resp.json()
        assert {d["name"] for d in body["devices"]} == {"guests-laptop"}
    _run(_with_client(flow))


def test_revoke_endpoint_kills_the_token(env):
    async def flow(client):
        device_uuid, token = _link()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post("/api/v1/devices/revoke",
                                 json={"device_id": device_uuid},
                                 headers=headers)
        assert resp.status == 200
        resp = await client.get("/api/v1/devices", headers=headers)
        assert resp.status == 401
    _run(_with_client(flow))


def test_today_is_scoped_to_the_devices_user(env):
    async def flow(client):
        _, token = _link(U1)
        resp = await client.get(
            "/api/v1/today", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert body["user"]["name"] == "megan"
    _run(_with_client(flow))


# --- the link_device tool (the production mint path) --------------------------


def test_link_device_tool_mints_a_working_code(env):
    from dainframe.tools.context import ToolContext
    from src.services.tools.link_tools import LINK_DEVICE

    ctx = ToolContext(stream_id=U1, activation_id="test-act",
                      actor="chordial", metadata={"scope": "dm"})
    result = _run(LINK_DEVICE.handler({}, ctx))
    assert "device link code:" in result
    code = result.split("device link code:")[1].split()[0]
    linked = device_auth.link_device(code, "megans-mac")
    assert linked is not None
    identity = device_auth.verify_device_token(linked[1])
    assert identity.user_uuid == U1


def test_link_device_tool_refuses_outside_dm(env):
    from dainframe.tools.context import ToolContext
    from src.services.tools.link_tools import LINK_DEVICE

    ctx = ToolContext(stream_id=U1, activation_id="test-act",
                      actor="chordial", metadata={"scope": "group"})
    result = _run(LINK_DEVICE.handler({}, ctx))
    assert result.startswith("refused")
    with env() as s:
        from src.database.models import LinkCode
        assert s.query(LinkCode).count() == 0


def test_link_device_tool_is_registered(env):
    from src.services.tools import build_default_registry
    registry = build_default_registry()
    assert "link_device" in {d.name for d in registry.definitions()}


# --- /api/v1 CORS (the tauri webview's path in) -------------------------------


def test_cors_preflight_and_headers_for_allowed_origin(env):
    async def flow(client):
        origin = "tauri://localhost"
        resp = await client.options(
            "/api/v1/sync/events",
            headers={"Origin": origin,
                     "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization"})
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == origin
        assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]

        # the real request carries the headers too, even on an error status
        resp = await client.get("/api/v1/today", headers={"Origin": origin})
        assert resp.status == 401
        assert resp.headers["Access-Control-Allow-Origin"] == origin
    _run(_with_client(flow))


def test_cors_refuses_unknown_origins(env):
    async def flow(client):
        resp = await client.options(
            "/api/v1/sync/events",
            headers={"Origin": "https://evil.example",
                     "Access-Control-Request-Method": "POST"})
        assert resp.status == 403

        _, token = _link()
        resp = await client.get(
            "/api/v1/today",
            headers={"Authorization": f"Bearer {token}",
                     "Origin": "https://evil.example"})
        # the request itself succeeds (bearer auth is the real gate) but no
        # CORS headers are granted, so a browser page cannot read the body
        assert resp.status == 200
        assert "Access-Control-Allow-Origin" not in resp.headers
    _run(_with_client(flow))


def test_origin_guard_does_not_block_v1_in_public_mode(env, monkeypatch):
    monkeypatch.setattr(Config, "WEB_PUBLIC_URL", "https://focus.example")
    monkeypatch.setattr(Config, "WEB_SESSION_SECRET", "test-secret")

    async def flow(client):
        _, token = _link()
        resp = await client.post(
            "/api/v1/sync/events",
            json={"events": [_event(1)]},
            headers={"Authorization": f"Bearer {token}",
                     "Origin": "tauri://localhost"})
        assert resp.status == 200
        body = await resp.json()
        assert body["acked_seq"] == 1
    _run(_with_client(flow))
