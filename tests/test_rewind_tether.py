"""rewind slice C (docs/REWIND_DESIGN.md section 8): the tether, server side.

the fold (rewind.* events -> one tracking row per offer, through
focus_flow's exactly-once claim), the sweep (opt-in, away threshold, quiet
hours, staleness, one-ping claim), the answer (first answer wins, tenant
checked), and the return channel (GET /api/v1/sync/decisions under device
auth). the sidecar half - applying or refusing a decision - lives in
tests/test_rewind.py with the rest of the sidecar rig.

times are fixed (a hand clock injected into the tether, fixed occurred_at
on the events) so quiet hours and thresholds are deterministic.
"""
import asyncio
import sys
import tempfile
import uuid as uuid_mod
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from config import Config
from src.database.models import (
    Base,
    PlatformIdentity,
    RewindDecision,
    User,
)
from src.managers.user_manager import UserManager
from src.services import device_sync, focus_flow, rewind_tether
from src.services.rewind_tether import RewindTether
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"
U2 = "user-two"

# midday utc: comfortably outside the default 21->8 quiet window
NOON = datetime(2026, 8, 17, 12, 0)


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(Config, "QUIET_HOURS_START", 21)
    monkeypatch.setattr(Config, "QUIET_HOURS_END", 8)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="megan", timezone="UTC",
                   schedule_preferences={"rewind_tether": True}))
        s.add(User(uuid=U2, preferred_name="guest", timezone="UTC"))
        s.add(PlatformIdentity(user_uuid=U1, platform="telegram",
                               platform_user_id="555", is_active=True))
        s.commit()
    yield TestSession
    engine.dispose()


class FakeRouter:
    """records tether pings; `result` plays a delivered / failed send."""

    def __init__(self, result=True):
        self.result = result
        self.sent = []

    def platforms(self):
        return ["telegram", "app"]

    async def deliver_choice_as(self, platform, target_id, message,
                                choices, speaker):
        self.sent.append({"platform": platform, "target": target_id,
                          "message": message, "choices": choices,
                          "speaker": speaker})
        return self.result


def _tether(router, now=NOON):
    return RewindTether(router, UserManager(), clock=lambda: now)


def _link(user=U1, name="test-device"):
    code = device_auth.mint_device_link_code(user)
    result = device_auth.link_device(code, name)
    assert result is not None
    return result  # (device_uuid, token)


_seq_counter = {"n": 0}


def _land(device_id, etype, payload, occurred):
    """one device event through the real pipeline: exactly-once apply,
    then the drain that folds it."""
    _seq_counter["n"] += 1
    result = device_sync.apply_events(device_id, [{
        "id": str(uuid_mod.uuid4()),
        "seq": _seq_counter["n"],
        "type": etype,
        "payload": payload,
        "occurred_at": occurred.isoformat(),
    }])
    assert not result.rejected
    focus_flow.drain()


def _offered(device_id, offer_uuid, occurred, contested=600,
             label="novel"):
    _land(device_id, "rewind.offered",
          {"offer_uuid": offer_uuid, "task_id": 7, "label": label,
           "contested_seconds": contested}, occurred)


@pytest.fixture()
def device(env):
    _seq_counter["n"] = 0
    device_uuid, token = _link()
    with db_mod.get_db() as db:
        from src.database.models import Device
        device_id = db.query(Device.id).filter(
            Device.device_uuid == device_uuid).scalar()
    return device_id, token


def _row(offer_uuid):
    with db_mod.get_db() as db:
        row = db.query(RewindDecision).filter(
            RewindDecision.offer_uuid == offer_uuid).first()
        if row is None:
            return None
        db.expunge(row)
        return row


def _run(coro):
    return asyncio.run(coro)


# --- the fold ----------------------------------------------------------------


def test_offered_folds_into_one_tracking_row(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    row = _row("o1")
    assert row is not None
    assert row.user_uuid == U1
    assert row.label == "novel"
    assert row.contested_seconds == 600
    assert row.closed_at is None and row.choice is None

    # a re-emit (the question's span grew) re-arms the away clock
    _offered(device_id, "o1", NOON - timedelta(minutes=5), contested=1500)
    row = _row("o1")
    assert row.contested_seconds == 1500
    assert row.offered_at == NOON - timedelta(minutes=5)


def test_terminal_events_close_the_row(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    _land(device_id, "rewind.applied",
          {"offer_uuid": "o1", "removed_seconds": 900},
          NOON - timedelta(minutes=1))
    assert _row("o1").closed_at is not None

    _offered(device_id, "o2", NOON - timedelta(minutes=30))
    _land(device_id, "rewind.kept", {"offer_uuid": "o2"}, NOON)
    assert _row("o2").closed_at is not None


def test_undo_rearms_the_row_fresh(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(hours=1))
    _land(device_id, "rewind.applied", {"offer_uuid": "o1"},
          NOON - timedelta(minutes=30))
    _land(device_id, "rewind.undone", {"offer_uuid": "o1"},
          NOON - timedelta(minutes=29))
    row = _row("o1")
    assert row.closed_at is None
    assert row.choice is None and row.pinged_at is None
    # undo happens at the desk: a fresh away clock from the undo, and the
    # person is marked present right now
    assert row.offered_at == NOON - timedelta(minutes=29)
    assert row.returned_at == row.offered_at


# --- the sweep ---------------------------------------------------------------


def test_the_ping_waits_for_the_away_threshold(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=10))
    router = FakeRouter()
    assert _run(_tether(router).sweep_once()) == 0
    assert router.sent == []

    _offered(device_id, "o2", NOON - timedelta(minutes=30))
    assert _run(_tether(router).sweep_once()) == 1
    assert len(router.sent) == 1
    assert router.sent[0]["platform"] == "telegram"
    assert router.sent[0]["target"] == "555"


def test_one_ping_never_two(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    router = FakeRouter()
    tether = _tether(router)
    assert _run(tether.sweep_once()) == 1
    assert _run(tether.sweep_once()) == 0
    assert len(router.sent) == 1
    assert _row("o1").ping_platform == "telegram"


def test_the_tether_is_separately_opt_in(device):
    """a linked phone alone must never earn a ping - only the explicit
    preference does (sol's design review)."""
    device_id, _ = device
    with db_mod.get_db() as db:
        db.query(User).filter(User.uuid == U1).update(
            {User.schedule_preferences: {}})
        db.commit()
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    router = FakeRouter()
    assert _run(_tether(router).sweep_once()) == 0
    assert router.sent == []
    assert _row("o1").pinged_at is None


def test_a_return_hands_the_question_back_to_the_desk(device):
    """the person came back: the card is showing, and a phone ping on top
    of it would be a second voice (one gentle surface)."""
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=40))
    _land(device_id, "return.detected", {}, NOON - timedelta(minutes=28))
    router = FakeRouter()
    assert _run(_tether(router).sweep_once()) == 0
    assert router.sent == []


def test_quiet_hours_hold_the_ping(device):
    """a ping about a quiet block must not be what wakes anyone up. the
    row stays unclaimed - and expires via staleness before morning."""
    device_id, _ = device
    late = datetime(2026, 8, 17, 23, 30)   # 23:30 utc, inside 21->8
    _offered(device_id, "o1", late - timedelta(minutes=30))
    router = FakeRouter()
    assert _run(_tether(router, now=late).sweep_once()) == 0
    assert _row("o1").pinged_at is None


def test_stale_questions_never_ping(device):
    """yesterday's quiet is the chip's business, not a notification's."""
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(hours=7))
    router = FakeRouter()
    assert _run(_tether(router).sweep_once()) == 0
    assert router.sent == []


def test_the_ping_speaks_impact_with_todays_estimate(device):
    """contested at mint plus the uninterrupted quiet since: 600s + 30m =
    'about 40m'. the device still derives the real number at apply time."""
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30), contested=600)
    router = FakeRouter()
    assert _run(_tether(router).sweep_once()) == 1
    ping = router.sent[0]
    assert "quiet about 40m" in ping["message"]
    assert '"novel"' in ping["message"]
    labels = [c["label"] for c in ping["choices"]]
    assert labels == ["remove ~40m & pause", "keep all time"]
    assert [c["data"] for c in ping["choices"]] == \
        ["rw:o1:remove", "rw:o1:keep"]
    # neutral copy: quiet is observed, never judged
    for banned in ("wandered", "left", "distracted", "working?"):
        assert banned not in ping["message"]


def test_a_failed_send_unclaims_for_another_try(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    router = FakeRouter(result=False)
    tether = _tether(router)
    assert _run(tether.sweep_once()) == 0
    assert _row("o1").pinged_at is None
    router.result = True
    assert _run(tether.sweep_once()) == 1
    assert len(router.sent) == 2


# --- the answer --------------------------------------------------------------


def test_first_answer_wins_at_the_intake(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    ack = rewind_tether.record_decision_token("telegram", "555",
                                              "rw:o1:remove")
    assert "got it" in ack
    row = _row("o1")
    assert row.choice == "remove" and row.source == "telegram"

    second = rewind_tether.record_decision_token("telegram", "555",
                                                 "rw:o1:keep")
    assert "first answer" in second
    assert _row("o1").choice == "remove"


def test_a_tap_is_tenant_checked(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    # an unlinked account
    ack = rewind_tether.record_decision_token("telegram", "999",
                                              "rw:o1:remove")
    assert "recognize" in ack
    # another user's linked account must not answer megan's question
    with db_mod.get_db() as db:
        db.add(PlatformIdentity(user_uuid=U2, platform="telegram",
                                platform_user_id="777", is_active=True))
        db.commit()
    ack = rewind_tether.record_decision_token("telegram", "777",
                                              "rw:o1:remove")
    assert "can't find" in ack
    assert _row("o1").choice is None


def test_the_desk_beats_a_late_tap(device):
    device_id, _ = device
    _offered(device_id, "o1", NOON - timedelta(minutes=30))
    _land(device_id, "rewind.kept", {"offer_uuid": "o1"}, NOON)
    ack = rewind_tether.record_decision_token("telegram", "555",
                                              "rw:o1:remove")
    assert "desk answered first" in ack
    assert _row("o1").choice is None


def test_a_garbled_token_is_refused(device):
    for token in ("rw:o1", "rw:o1:maybe", "xx:o1:keep", ""):
        ack = rewind_tether.record_decision_token("telegram", "555", token)
        assert "stale" in ack


# --- the return channel ------------------------------------------------------


async def _with_client(fn):
    service = WebService(user_resolver=lambda: U1)
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def test_decisions_endpoint_serves_only_decided_unclosed_rows(device):
    device_id, token = device
    now = NOON
    with db_mod.get_db() as db:
        db.add(RewindDecision(user_uuid=U1, offer_uuid="decided",
                              offered_at=now, choice="remove",
                              decided_at=now, source="telegram"))
        db.add(RewindDecision(user_uuid=U1, offer_uuid="undecided",
                              offered_at=now))
        db.add(RewindDecision(user_uuid=U1, offer_uuid="closed",
                              offered_at=now, choice="keep",
                              decided_at=now, closed_at=now))
        db.add(RewindDecision(user_uuid=U2, offer_uuid="foreign",
                              offered_at=now, choice="remove",
                              decided_at=now))
        db.commit()

    async def flow(client):
        resp = await client.get("/api/v1/sync/decisions")
        assert resp.status == 401

        resp = await client.get(
            "/api/v1/sync/decisions",
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert [d["offer_uuid"] for d in body["decisions"]] == ["decided"]
        assert body["decisions"][0]["choice"] == "remove"
        assert body["decisions"][0]["source"] == "telegram"
    _run(_with_client(flow))
