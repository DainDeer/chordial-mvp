"""the taper (phase 6c, ROOMS_DESIGN.md section 7).

the invariants under test:

- quiet is EARNED: each consecutive steady cycle doubles the check-in
  interval; the streak reads newest-first by the cycle's seal, never by
  when the card happened to be filed.
- a wobbly cycle resets to full coaching; an unjudged cycle (scores that
  could not be computed) earns nothing either - absence of evidence
  never extends the quiet.
- unsustainable bursts fail the steady test even with perfect numbers.
- presence never tapers to zero: the multiplier caps, and the cap is the
  "keeping watch" posture.
- the pulse hands out the stretched beat per user, and a taper failure
  degrades to the flat base beat - never a stalled check-in.
- the ambient line exists only once quiet is earned: a fresh user's
  prompt bytes are exactly the pre-6c bytes (the golden suites pin the
  same fact from the other side).
- the api read model is device-authed and tenant-scoped.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from config import Config
from src.database.models import Assessment, Base, User
from src.services import taper
from src.services.pulse_wiring import ChordialPulseSource, checkin_rhythm
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"
U2 = "user-two"

NOW = datetime(2026, 8, 18, 12, 0)


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


def _run(coro):
    return asyncio.run(coro)


def _card(subject_id, sealed_days_ago, *, consistency=0.8, execution=0.8,
          sustainability=0.9, title=None, created_at=None):
    """one detached assessment row, shaped like cycle_scorer reads them.
    None for a component score = the scorer could not compute it."""
    sealed = (NOW - timedelta(days=sealed_days_ago)).isoformat()
    components = {}
    for name, score in (("consistency", consistency),
                        ("execution", execution),
                        ("sustainability", sustainability)):
        components[name] = {"score": score, "evidence": []}
    return {
        "subject_id": subject_id,
        "summary": "the record, briefly",
        "created_at": created_at or sealed,
        "detail": {
            "components": components,
            "cycle": {"title": title or subject_id, "sealed_at": sealed},
        },
    }


# --- the arithmetic ----------------------------------------------------------


def test_a_fresh_ledger_is_settling_in():
    decision = taper.assess([])
    assert decision["posture"] == taper.SETTLING_IN
    assert decision["streak"] == 0
    assert decision["multiplier"] == 1


def test_one_steady_cycle_doubles_the_quiet():
    decision = taper.assess([_card("c1", 2)])
    assert decision["streak"] == 1
    assert decision["multiplier"] == 2
    assert decision["posture"] == taper.RHYTHM
    assert decision["judged"][0]["verdict"] == "steady"


def test_consecutive_steady_cycles_compound():
    decision = taper.assess([_card("c2", 2), _card("c1", 16)])
    assert decision["streak"] == 2
    assert decision["multiplier"] == 4


def test_the_cap_is_keeping_watch():
    cards = [_card(f"c{n}", n * 14) for n in range(1, 5)]
    decision = taper.assess(cards)
    assert decision["streak"] == 4
    assert decision["multiplier"] == Config.TAPER_MAX_MULTIPLIER
    assert decision["posture"] == taper.KEEPING_WATCH


def test_a_wobbly_latest_cycle_resets_to_full_coaching():
    decision = taper.assess([
        _card("c2", 2, consistency=0.2),
        _card("c1", 16),
    ])
    assert decision["streak"] == 0
    assert decision["multiplier"] == 1
    assert decision["posture"] == taper.BUILDING
    assert decision["judged"][0]["verdict"] == "wobbly"


def test_an_unjudged_cycle_earns_nothing():
    decision = taper.assess([
        _card("c2", 2, consistency=None, execution=None),
        _card("c1", 16),
    ])
    assert decision["judged"][0]["verdict"] == "unjudged"
    assert decision["streak"] == 0
    assert decision["multiplier"] == 1


def test_the_streak_stops_at_the_first_break():
    decision = taper.assess([
        _card("c3", 2),
        _card("c2", 16, execution=0.1),
        _card("c1", 30),
    ])
    assert decision["streak"] == 1
    assert decision["multiplier"] == 2


def test_bursts_dont_earn_quiet():
    decision = taper.assess([_card("c1", 2, sustainability=0.2)])
    assert decision["judged"][0]["verdict"] == "wobbly"
    assert decision["multiplier"] == 1
    assert any("bursts" in r for r in decision["judged"][0]["reasons"])


def test_the_ledger_orders_by_seal_not_filing():
    # the older cycle was scored LATE (backfill) - its card is the newest
    # filing, but the newest evidence is still the steady recent cycle
    late_filed_old = _card("c1", 30, consistency=0.1,
                           created_at=NOW.isoformat())
    decision = taper.assess([late_filed_old, _card("c2", 2)])
    assert decision["judged"][0]["subject_id"] == "c2"
    assert decision["streak"] == 1


# --- the ambient line --------------------------------------------------------


def test_the_ambient_line_stays_silent_until_quiet_is_earned():
    assert taper.ambient_line(taper.assess([])) is None
    wobbly = taper.assess([_card("c1", 2, consistency=0.1)])
    assert taper.ambient_line(wobbly) is None


def test_the_ambient_line_speaks_the_earned_beat():
    decision = taper.assess([_card("c2", 2), _card("c1", 16)])
    decision["checkin_minutes"] = 60 * decision["multiplier"]
    line = taper.ambient_line(decision)
    assert "rhythm" in line
    assert "4 hours" in line
    assert "earned" in line


def test_the_keeping_watch_line_reserves_the_voice():
    cards = [_card(f"c{n}", n * 14) for n in range(1, 5)]
    decision = taper.assess(cards)
    decision["checkin_minutes"] = 60 * decision["multiplier"]
    line = taper.ambient_line(decision)
    assert "keeping watch" in line
    assert "early-warning" in line


# --- the ledger read ---------------------------------------------------------


def _file_card(user, card):
    with db_mod.SessionLocal() as s:
        s.add(Assessment(
            user_uuid=user, helper_id="edwin", subject_type="cycle",
            subject_id=card["subject_id"], summary=card["summary"],
            detail=card["detail"],
            created_at=datetime.fromisoformat(card["created_at"])))
        s.commit()


def test_state_for_reads_only_this_users_ledger(env):
    _file_card(U2, _card("c1", 2))
    _file_card(U2, _card("c2", 16))
    state = taper.state_for(U1, 60)
    assert state["posture"] == taper.SETTLING_IN
    assert state["checkin_minutes"] == 60

    _file_card(U1, _card("c3", 2))
    state = taper.state_for(U1, 60)
    assert state["multiplier"] == 2
    assert state["checkin_minutes"] == 120
    assert state["base_minutes"] == 60


def test_the_ambient_read_speaks_only_when_tapered(env):
    assert taper.ambient_line_for(U1) is None
    _file_card(U1, _card("c1", 2))
    line = taper.ambient_line_for(U1)
    assert line is not None and "rhythm" in line


# --- the pulse seam ----------------------------------------------------------


def _source(minutes_fn):
    user_manager = SimpleNamespace(
        get_scheduled_users=_async_return([U1]))
    return ChordialPulseSource(user_manager, checkin_minutes=minutes_fn)


def _async_return(value):
    async def fn(*a, **kw):
        return value
    return fn


def _checkin_interval(streams):
    (user, rhythms), = streams
    assert user == U1
    (rhythm,) = [r for r in rhythms if r.rhythm_id == "checkin"]
    return rhythm.rhythm.every


def test_the_pulse_hands_out_the_stretched_beat():
    source = _source(lambda user: 120)
    every = _checkin_interval(_run(source.streams()))
    assert every == timedelta(minutes=120)


def test_an_untapered_source_keeps_the_flat_base_beat():
    source = _source(None)
    every = _checkin_interval(_run(source.streams()))
    assert every == timedelta(minutes=Config.DM_INTERVAL_MINUTES)


def test_a_taper_failure_never_stalls_the_checkin():
    def broken(user):
        raise RuntimeError("the ledger is on fire")
    source = _source(broken)
    every = _checkin_interval(_run(source.streams()))
    assert every == timedelta(minutes=Config.DM_INTERVAL_MINUTES)


def test_the_default_rhythm_is_byte_for_byte_the_old_beat():
    assert (checkin_rhythm().rhythm.every
            == timedelta(minutes=Config.DM_INTERVAL_MINUTES))
    assert checkin_rhythm(None).rhythm.every == checkin_rhythm().rhythm.every


# --- the api read model ------------------------------------------------------


def _device_token(user=U1):
    code = device_auth.mint_device_link_code(user)
    _, token = device_auth.link_device(code, "test")
    return token


async def _with_service(fn, **kw):
    client = TestClient(TestServer(
        WebService(user_resolver=lambda: U1, **kw).build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def test_the_arc_endpoint_requires_a_device_token(env):
    async def flow(client):
        assert (await client.get("/api/v1/arc")).status == 401
    _run(_with_service(flow))


def test_the_arc_endpoint_reports_earned_quiet(env):
    _file_card(U1, _card("c2", 2))
    _file_card(U1, _card("c1", 16))
    _file_card(U2, _card("c9", 2, consistency=0.1))
    token = _device_token()

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/arc", headers=headers)
        assert resp.status == 200
        arc = (await resp.json())["arc"]
        assert arc["posture"] == taper.RHYTHM
        assert arc["streak"] == 2
        assert arc["multiplier"] == 4
        assert arc["checkin_minutes"] == Config.DM_INTERVAL_MINUTES * 4
        assert [j["subject_id"] for j in arc["judged"]] == ["c2", "c1"]
    _run(_with_service(flow))
