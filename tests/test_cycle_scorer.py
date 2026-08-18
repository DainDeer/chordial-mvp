"""edwin's cycle scorer (phase 6, ROOMS_DESIGN.md section 8).

the honesty contract under test:
- discovery finds cycles whose window is OVER (complete, or active with
  the end date behind the user's local today) and never the still-running,
  the never-run, or the ancient (backfill window)
- the scores in a filed assessment are byte-identical to the deterministic
  scorecard - the model's only contribution is prose
- a finding citing a ref that doesn't resolve against the gathered rows is
  structurally unrecordable: dropped and counted, never filed
- a dead or absent model still files the scorecard, with the deterministic
  summary - arithmetic never waits on prose
- exactly one assessment per (user, 'cycle', public_id), whatever races
- the model's spend lands on edwin's ledger (role='scorer')
- GET /api/v1/scorecards is device-authed and tenant-scoped
"""
import asyncio
import json
import sys
import tempfile
import uuid as uuid_mod
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Assessment, Base, DeviceEvent, User
from src.services.cycle_scorer import (CycleScorer, recent_assessments)
from src.services.cycles import CycleStore
from src.services.workspace import get_store
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"
U2 = "user-two"

NOW = datetime(2026, 8, 18, 12, 0)          # the scorer's clock, fixed
TODAY = NOW.date()


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


class FakeProvider:
    """the utility model, scripted: returns fixed text or raises."""

    def __init__(self, text=None, exc=None):
        self.model = "fake-utility"
        self.text = text
        self.exc = exc
        self.requests = []

    async def create_message(self, request):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                                cache_read_tokens=0, cache_write_tokens=0)
        return SimpleNamespace(text=self.text, usage=usage)


class FakeRecorder:
    def __init__(self):
        self.calls = []

    def record_call(self, **kwargs):
        self.calls.append(kwargs)


def _scorer(provider=None, recorder=None):
    return CycleScorer(provider=provider, provider_name="fake",
                       usage_recorder=recorder or FakeRecorder(),
                       clock=lambda: NOW)


def _ended_cycle(user=U1, *, days=14, ended_days_ago=1, status="complete",
                 **kw):
    """a cycle whose window closed `ended_days_ago` days before NOW."""
    end = TODAY - timedelta(days=ended_days_ago)
    start = end - timedelta(days=days - 1)
    defaults = dict(status=status, start_date=start.isoformat(),
                    end_date=end.isoformat(), theme="steady",
                    capacity_blocks=12)
    defaults.update(kw)
    return get_store().create_cycle(user, "cycle t", **defaults)


_SEQ = iter(range(1, 10_000))


def _device_pk(env, user=U1):
    code = device_auth.mint_device_link_code(user)
    device_uuid, token = device_auth.link_device(code, "test")
    from src.database.models import Device
    with env() as s:
        pk = s.query(Device.id).filter(
            Device.device_uuid == device_uuid).scalar()
    return pk, token


def _bank(env, user, device_pk, *, task_id, seconds, when):
    with env() as s:
        s.add(DeviceEvent(
            event_uuid=str(uuid_mod.uuid4()), device_id=device_pk,
            user_uuid=user, seq=next(_SEQ), event_type="session.ended",
            payload={"task_id": task_id, "seconds": seconds},
            occurred_at=when, rejected=False))
        s.commit()


def _committed_cycle(env, cs=None, **cycle_kw):
    """an ended, frozen cycle with one committed task and banked work."""
    cs = cs or CycleStore()
    ws = get_store()
    cyc = _ended_cycle(**cycle_kw)
    task = ws.create_task(U1, "bounce stems")
    cs.create_commitment(U1, cyc["id"], "mix track one", priority="high",
                         blocks_planned=4, task_id=task["id"])
    cs.freeze_baseline(U1, cyc["id"])
    device_pk, _ = _device_pk(env)
    start = date.fromisoformat(cyc["start_date"])
    for d in range(3):
        _bank(env, U1, device_pk, task_id=task["id"], seconds=1500,
              when=datetime.combine(start + timedelta(days=d),
                                    datetime.min.time())
              + timedelta(hours=14))
    return cyc


# --- discovery ---------------------------------------------------------------


def test_discovery_finds_ended_unassessed_cycles_only(env):
    ended = _ended_cycle()                                    # complete -> due
    _ended_cycle(status="active", ended_days_ago=-5)          # still running
    _ended_cycle(status="upcoming")                           # never ran
    due = _scorer().cycles_due()
    assert due == [(U1, ended["id"])]


def test_an_active_cycle_past_its_end_date_is_due(env):
    lingering = _ended_cycle(status="active", ended_days_ago=2)
    due = _scorer().cycles_due()
    assert due == [(U1, lingering["id"])]


def test_the_backfill_window_leaves_ancient_cycles_unscored(env):
    from src.database.models import Cycle
    ancient = _ended_cycle(ended_days_ago=40)
    with db_mod.SessionLocal() as s:      # closed back when it actually ended
        s.query(Cycle).filter(Cycle.id == ancient["id"]).update(
            {"closed_at": NOW - timedelta(days=40)})
        s.commit()
    _ended_cycle(status="active", ended_days_ago=40)   # no closed_at at all
    assert _scorer().cycles_due() == []


def test_an_assessed_cycle_is_never_rediscovered(env):
    _committed_cycle(env)
    scorer = _scorer()
    (user, cycle_id), = scorer.cycles_due()
    assert _run(scorer.score_cycle(user, cycle_id)) is not None
    assert scorer.cycles_due() == []


# --- scoring + filing --------------------------------------------------------


def test_a_scorecard_files_without_any_model_at_all(env):
    cyc = _committed_cycle(env)
    scorer = _scorer(provider=None)
    filed = _run(scorer.score_cycle(U1, cyc["id"]))
    assert filed is not None
    detail = filed["detail"]
    assert detail["summary_source"] == "deterministic"
    assert detail["components"]["execution"]["score"] == 0.75
    assert "3.0 of 4 planned blocks landed" in \
        detail["components"]["execution"]["evidence"]
    assert "3.0 of 4 planned blocks landed" in filed["summary"]
    rows = recent_assessments(U1)
    assert len(rows) == 1
    assert rows[0]["helper_id"] == "edwin"
    assert rows[0]["subject_type"] == "cycle"
    assert rows[0]["subject_id"] == filed["subject_id"]


def test_the_model_writes_prose_and_never_touches_the_numbers(env):
    cs = CycleStore()
    cyc = _committed_cycle(env, cs=cs)
    commitment_uuid = cs.list_commitments(U1, cycle_id=cyc["id"])[0]["uuid"]
    reply = json.dumps({
        "summary": "the plan held; one estimate ran a block long.",
        "findings": [
            {"claim": "the mix estimate was one block optimistic",
             "evidence": [f"cm:{commitment_uuid}", "component:estimation"],
             "suggested_action": "plan 3 blocks next cycle"},
        ],
    })
    scorer = _scorer(provider=FakeProvider(text=reply))
    filed = _run(scorer.score_cycle(U1, cyc["id"]))
    detail = filed["detail"]
    assert detail["summary_source"] == "model"
    assert filed["summary"] == "the plan held; one estimate ran a block long."
    assert len(detail["findings"]) == 1
    assert detail["findings"][0]["suggested_action"] == \
        "plan 3 blocks next cycle"
    # the numbers are the deterministic scorecard's, untouched by prose
    assert detail["components"]["execution"]["score"] == 0.75
    assert detail["dropped_findings"] == 0


def test_a_finding_citing_nothing_is_structurally_unrecordable(env):
    cs = CycleStore()
    cyc = _committed_cycle(env, cs=cs)
    commitment_uuid = cs.list_commitments(U1, cycle_id=cyc["id"])[0]["uuid"]
    reply = json.dumps({
        "summary": "two claims, one honest.",
        "findings": [
            {"claim": "the real one",
             "evidence": [f"cm:{commitment_uuid}"]},
            {"claim": "the invented one",
             "evidence": ["cm:no-such-commitment"]},
            {"claim": "the unanchored one", "evidence": []},
        ],
    })
    filed = _run(_scorer(provider=FakeProvider(text=reply))
                 .score_cycle(U1, cyc["id"]))
    detail = filed["detail"]
    assert [f["claim"] for f in detail["findings"]] == ["the real one"]
    assert detail["dropped_findings"] == 2


def test_a_dead_model_still_files_the_scorecard(env):
    cyc = _committed_cycle(env)
    scorer = _scorer(provider=FakeProvider(exc=RuntimeError("model down")))
    filed = _run(scorer.score_cycle(U1, cyc["id"]))
    assert filed is not None
    assert filed["detail"]["summary_source"] == "deterministic"
    assert filed["detail"]["components"]["execution"]["score"] == 0.75


def test_garbled_model_output_degrades_to_the_deterministic_summary(env):
    cyc = _committed_cycle(env)
    filed = _run(_scorer(provider=FakeProvider(text="not json at all"))
                 .score_cycle(U1, cyc["id"]))
    assert filed["detail"]["summary_source"] == "deterministic"
    assert filed["detail"]["findings"] == []


def test_exactly_one_assessment_whatever_races(env):
    cyc = _committed_cycle(env)
    scorer = _scorer()
    assert _run(scorer.score_cycle(U1, cyc["id"])) is not None
    # a second sweep - and a raw double-file - both lose quietly
    assert _run(scorer.score_cycle(U1, cyc["id"])) is None
    subject_id = recent_assessments(U1)[0]["subject_id"]
    assert scorer._file(U1, subject_id, "the racer", {}) is False
    rows = recent_assessments(U1)
    assert len(rows) == 1
    assert rows[0]["summary"] != "the racer"


def test_the_scorers_spend_lands_on_edwins_ledger(env):
    cyc = _committed_cycle(env)
    recorder = FakeRecorder()
    reply = json.dumps({"summary": "fine.", "findings": []})
    _run(_scorer(provider=FakeProvider(text=reply), recorder=recorder)
         .score_cycle(U1, cyc["id"]))
    call, = recorder.calls
    assert call["role"] == "scorer"
    assert call["helper_id"] == "edwin"
    assert call["user_uuid"] == U1


def test_a_sweep_scores_everything_due(env):
    _committed_cycle(env)
    _ended_cycle(ended_days_ago=3)      # empty and unfrozen, still scored
    scorer = _scorer()
    filed = _run(scorer.sweep_once())
    assert filed == 2
    subjects = {r["subject_id"] for r in recent_assessments(U1)}
    assert len(subjects) == 2
    assert scorer.cycles_due() == []


# --- the read model ----------------------------------------------------------


def _device_token(env, user=U1):
    code = device_auth.mint_device_link_code(user)
    _, token = device_auth.link_device(code, "test")
    return token


async def _with_service(fn):
    client = TestClient(TestServer(
        WebService(user_resolver=lambda: U1).build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def test_scorecards_endpoint_requires_a_device_token(env):
    async def flow(client):
        resp = await client.get("/api/v1/scorecards")
        assert resp.status == 401
    _run(_with_service(flow))


def test_scorecards_endpoint_serves_only_the_devices_user(env):
    cyc = _committed_cycle(env)
    _run(_scorer().score_cycle(U1, cyc["id"]))
    with db_mod.SessionLocal() as s:
        s.add(Assessment(user_uuid=U2, helper_id="edwin",
                         subject_type="cycle", subject_id="cy999",
                         summary="someone else's cycle"))
        s.commit()
    token = _device_token(env)

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/scorecards", headers=headers)
        assert resp.status == 200
        body = await resp.json()
        assert len(body["assessments"]) == 1
        row = body["assessments"][0]
        assert row["helper_id"] == "edwin"
        assert "someone else" not in row["summary"]
        assert row["detail"]["components"]["execution"]["score"] == 0.75
    _run(_with_service(flow))
