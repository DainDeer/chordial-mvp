"""the cycle rooms (phase 6b, ROOMS_DESIGN.md sections 4+8).

the invariants under test:

- a retro and a planning room exist at most once per sealed cycle (the
  subject anchor is their uniqueness; null dates never guarded them), and
  opening one cycle room closes the user's other open cycle rooms - that
  close is what writes the summary planning hydrates. daily rooms are
  untouched by any of it.
- walking back into a settled cycle room REOPENS it (never a locked door
  with a composer painted on), and the reopened conversation's new
  exchanges recompress into the summary on the next close. the settle
  runs after the open commits, so a two-open race repairs instead of
  persisting.
- a room in 'closing' refuses turns at both boundaries: mid-digest is
  not open.
- THE RETRO IS A SUBPOENA: opening it scores the cycle on demand, waiving
  only the grace period - never the completeness requirement. the card
  records it sealed by the retro.
- edwin's presentation is seeded exactly once, quotes the filed card's
  arithmetic verbatim, and degrades to an honest "no card yet" note.
- the planning room seeds nothing and hydrates ITS cycle's retro summary
  plus the card render; a daily room never hydrates a retro summary (the
  family filter on latest_summary).
- the director casts the cycle family (vel/pip/mabel/edwin) in cycle
  rooms, exactly like legacy folds into daily.
- room-bound turns land in the bound room's stream; a closed room refuses
  honestly instead of silently rerouting to daily.
- the api doors are device-authed and tenant-scoped like everything else.
"""
import asyncio
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
from src.database.models import Base, ConversationEvent, Cycle, DeviceEvent, User
from src.models.unified_message import UnifiedMessage
from src.services.chat_service import CLOSED_ROOM_REPLY, ChatService
from src.services.cycle_rooms import CycleRooms
from src.services.cycle_scorer import (CycleScorer, assessment_for,
                                       render_assessment)
from src.services.cycles import CycleStore
from src.services.orchestration import ChordialContext, ChordialDirector
from src.services.rooms import RoomStore
from src.services.workspace import get_store
from src.managers.user_manager import UserManager
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"
U2 = "user-two"

NOW = datetime(2026, 8, 18, 12, 0)
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


def _scorer():
    """the deterministic scorer: no model, arithmetic files anyway."""
    return CycleScorer(provider=None, provider_name="fake",
                       usage_recorder=SimpleNamespace(
                           record_call=lambda **kw: None),
                       clock=lambda: NOW)


def _ended_cycle(user=U1, *, days=14, ended_days_ago=2, status="complete",
                 closed_hours_ago=None, **kw):
    end = TODAY - timedelta(days=ended_days_ago)
    start = end - timedelta(days=days - 1)
    defaults = dict(status=status, start_date=start.isoformat(),
                    end_date=end.isoformat(), theme="steady",
                    capacity_blocks=12)
    defaults.update(kw)
    cyc = get_store().create_cycle(user, "rewillow v1", **defaults)
    if status == "complete":
        closed = (NOW - timedelta(hours=closed_hours_ago)
                  if closed_hours_ago is not None
                  else NOW - timedelta(days=ended_days_ago))
        with db_mod.SessionLocal() as s:
            s.query(Cycle).filter(Cycle.id == cyc["id"]).update(
                {"closed_at": closed})
            s.commit()
    return cyc


_SEQ = iter(range(1, 10_000))


def _committed_cycle(env, **cycle_kw):
    """an ended, frozen cycle with one committed task and banked work
    (execution lands at 0.75 - the number the presentation must quote)."""
    cs = CycleStore()
    ws = get_store()
    cyc = _ended_cycle(**cycle_kw)
    task = ws.create_task(U1, "bounce stems")
    cs.create_commitment(U1, cyc["id"], "mix track one", priority="high",
                         blocks_planned=4, task_id=task["id"])
    cs.freeze_baseline(U1, cyc["id"])
    code = device_auth.mint_device_link_code(U1)
    device_uuid, _ = device_auth.link_device(code, "test")
    from src.database.models import Device
    with env() as s:
        device_pk = s.query(Device.id).filter(
            Device.device_uuid == device_uuid).scalar()
    start = date.fromisoformat(cyc["start_date"])
    with env() as s:
        for d in range(3):
            s.add(DeviceEvent(
                event_uuid=str(uuid_mod.uuid4()), device_id=device_pk,
                user_uuid=U1, seq=next(_SEQ), event_type="session.ended",
                payload={"task_id": task["id"], "seconds": 1500},
                rejected=False,
                occurred_at=datetime.combine(
                    start + timedelta(days=d), datetime.min.time())
                + timedelta(hours=14)))
        s.commit()
    return cyc


def _messages(stream_id):
    with db_mod.SessionLocal() as s:
        return s.query(ConversationEvent).filter(
            ConversationEvent.stream_id == stream_id,
            ConversationEvent.kind == "message",
        ).order_by(ConversationEvent.id).all()


# --- the store: exactly-once rooms, the close chain ---------------------------


def test_a_cycle_room_exists_at_most_once_per_cycle(env):
    store = RoomStore()
    first, created = store.open_cycle_room(U1, "cycle_retro", "c1")
    again, recreated = store.open_cycle_room(U1, "cycle_retro", "c1")
    assert created is True and recreated is False
    assert first["room_uuid"] == again["room_uuid"]
    assert first["subject_type"] == "cycle"
    assert first["subject_id"] == "c1"
    assert first["date"] is None


def test_opening_a_cycle_room_closes_the_other_open_one(env):
    store = RoomStore()
    retro, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    planning, _ = store.open_cycle_room(U1, "cycle_planning", "c1")
    assert store.get_by_uuid(retro["room_uuid"])["status"] == "closed"
    assert store.get_by_uuid(planning["room_uuid"])["status"] == "open"
    # the close compressed the retro into the summary planning hydrates
    assert store.summary_of(U1, "cycle_retro", "c1") is not None


def test_walking_back_into_your_open_room_closes_nothing(env):
    store = RoomStore()
    retro, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    store.open_cycle_room(U1, "cycle_retro", "c1")
    assert store.get_by_uuid(retro["room_uuid"])["status"] == "open"


def test_walking_back_into_a_settled_room_reopens_it(env):
    # planning -> retro -> planning: the pair is one conversation
    # revisited, never a locked door with a composer painted on
    store = RoomStore()
    planning, _ = store.open_cycle_room(U1, "cycle_planning", "c1")
    store.open_cycle_room(U1, "cycle_retro", "c1")
    assert store.get_by_uuid(planning["room_uuid"])["status"] == "closed"
    again, created = store.open_cycle_room(U1, "cycle_planning", "c1")
    assert created is False
    assert again["room_uuid"] == planning["room_uuid"]
    assert again["status"] == "open"
    # and the retro settled closed in turn
    retro = store.cycle_rooms_for(U1, "c1")["cycle_retro"]
    assert retro["status"] == "closed"


def test_a_reopened_conversation_reaches_the_summary(env):
    # the exchange added after a reopen must recompress into the summary
    # planning hydrates - a stale first digest would drop it
    store = RoomStore()
    retro, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    store.open_cycle_room(U1, "cycle_planning", "c1")     # retro closes, empty
    assert "0 user messages" in store.summary_of(U1, "cycle_retro", "c1")
    store.open_cycle_room(U1, "cycle_retro", "c1")        # reopen
    with db_mod.SessionLocal() as s:
        s.add(ConversationEvent(
            user_uuid=U1, stream_id=retro["room_uuid"], platform="app",
            author_type="user", author="user", kind="message",
            content="one more thing about tuesday"))
        s.commit()
    store.open_cycle_room(U1, "cycle_planning", "c1")     # retro closes again
    assert "1 user messages" in store.summary_of(U1, "cycle_retro", "c1")


def test_the_settle_repairs_a_two_open_race(env):
    # sol reproduced two devices opening different doors concurrently and
    # both rooms ending open. the settle now runs AFTER the open commits,
    # so whichever open lands later closes the other - and any two-open
    # state that ever exists repairs on the next walk-in
    store = RoomStore()
    retro, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    planning, _ = store.open_cycle_room(U1, "cycle_planning", "c1")
    from src.database.models import Room
    with db_mod.SessionLocal() as s:                  # force the bad state
        s.query(Room).filter(Room.room_uuid == retro["room_uuid"]).update(
            {"status": "open", "closed_at": None})
        s.commit()
    mine, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    assert store.get_by_uuid(mine["room_uuid"])["status"] == "open"
    assert store.get_by_uuid(planning["room_uuid"])["status"] == "closed"
    open_rooms = [r for r in store.cycle_rooms_for(U1, "c1").values()
                  if r and r["status"] == "open"]
    assert len(open_rooms) == 1


def test_a_late_line_in_a_closed_retro_reaches_planning(env):
    # a turn that raced the close (or landed while closing) folds into the
    # summary before planning hydration reads it - open_cycle_room runs
    # the stale-summary sweep first
    store = RoomStore()
    retro, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    store.open_cycle_room(U1, "cycle_planning", "c1")     # retro closes
    with db_mod.SessionLocal() as s:
        s.add(ConversationEvent(
            user_uuid=U1, stream_id=retro["room_uuid"], platform="app",
            author_type="user", author="user", kind="message",
            content="wait, one more thing"))
        s.commit()
    store.open_cycle_room(U1, "cycle_planning", "c1")
    assert "1 user messages" in store.summary_of(U1, "cycle_retro", "c1")


def test_daily_rooms_are_untouched_by_the_cycle_lifecycle(env):
    store = RoomStore()
    daily = store.current_room(U1, TODAY)
    store.open_cycle_room(U1, "cycle_retro", "c1")
    store.open_cycle_room(U1, "cycle_planning", "c1")
    assert store.get_by_uuid(daily["room_uuid"])["status"] == "open"


def test_another_users_cycle_rooms_are_not_mine(env):
    store = RoomStore()
    theirs, _ = store.open_cycle_room(U2, "cycle_retro", "c1")
    mine, created = store.open_cycle_room(U1, "cycle_retro", "c1")
    assert created is True
    assert mine["room_uuid"] != theirs["room_uuid"]
    assert store.get_by_uuid(theirs["room_uuid"])["status"] == "open"


def test_latest_summary_speaks_only_the_asked_family(env):
    # THE hydration caveat: a retro closing last must not become
    # "previously:" in tomorrow's daily room
    store = RoomStore()
    daily = store.current_room(U1, TODAY)
    store.close_room(daily["id"])
    retro, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    store.close_room(retro["id"])           # newer summary than the daily's
    assert store.latest_summary(U1)["room_type"] == "daily"
    assert store.latest_summary(
        U1, room_types=("cycle_retro",))["room_type"] == "cycle_retro"


def test_the_archive_carries_cycle_rooms_with_their_anchor(env):
    store = RoomStore()
    store.current_room(U1, TODAY)
    store.open_cycle_room(U1, "cycle_retro", "c1")
    rows = store.archive(U1)
    retro_rows = [r for r in rows if r["room_type"] == "cycle_retro"]
    assert len(retro_rows) == 1
    assert retro_rows[0]["subject_id"] == "c1"


# --- the doors ---------------------------------------------------------------


def test_no_sealed_cycle_means_no_doors(env):
    _ended_cycle(status="active", ended_days_ago=-5)
    doors = CycleRooms(scorer=_scorer())
    assert doors.doors(U1) is None
    assert _run(doors.open_retro(U1)) is None
    assert _run(doors.open_planning(U1)) is None


def test_the_retro_door_scores_on_demand_waiving_only_the_grace(env):
    # closed an hour ago: the sweep would wait out the 24h watermark, but
    # the person sitting down for the retro has called the question
    cyc = _committed_cycle(env, closed_hours_ago=1)
    scorer = _scorer()
    assert scorer.cycles_due() == []            # the sweep still waits
    doors = CycleRooms(scorer=scorer)
    result = _run(doors.open_retro(U1))
    assert result["created"] is True
    card = assessment_for(U1, f"c{cyc['id']}")
    assert card is not None
    assert card["detail"]["cycle"]["scored_by"] == "retro"
    assert card["detail"]["components"]["execution"]["score"] == 0.75


def test_the_presentation_quotes_the_filed_card_exactly_once(env):
    _committed_cycle(env)
    doors = CycleRooms(scorer=_scorer())
    result = _run(doors.open_retro(U1))
    _run(doors.open_retro(U1))                  # walking back in reseeds nothing
    rows = _messages(result["room"]["id"])
    assert len(rows) == 1
    assert rows[0].author == "edwin"
    assert rows[0].author_type == "agent"
    assert "execution 0.75" in rows[0].content
    assert "opens the ledger" in rows[0].content


def test_a_cardless_retro_says_so_instead_of_pretending(env):
    # no scorer wired (or scoring failed): the room still opens, with an
    # honest note instead of an invented card
    _committed_cycle(env)
    doors = CycleRooms(scorer=None)
    result = _run(doors.open_retro(U1))
    rows = _messages(result["room"]["id"])
    assert len(rows) == 1
    assert "isn't written yet" in rows[0].content
    assert assessment_for(U1, f"c{result['cycle']['id']}") is None


def test_the_presentation_catches_up_when_the_card_files_late(env):
    # scoring failed at first open (no scorer wired). a later open with a
    # working scorer must present the eventual card - the "arithmetic will
    # catch up" note is a promise, and the metadata marker is the durable
    # state that keeps it
    _committed_cycle(env)
    result = _run(CycleRooms(scorer=None).open_retro(U1))
    room_uuid = result["room"]["id"]
    assert len(_messages(room_uuid)) == 1               # the pending note

    repaired = _run(CycleRooms(scorer=_scorer()).open_retro(U1))
    assert repaired["room"]["id"] == room_uuid
    rows = _messages(room_uuid)
    assert len(rows) == 2
    assert "now written - as promised" in rows[1].content
    assert "execution 0.75" in rows[1].content

    _run(CycleRooms(scorer=_scorer()).open_retro(U1))   # idempotent
    assert len(_messages(room_uuid)) == 2


def test_a_crash_between_creation_and_seeding_repairs_on_open(env):
    # the room exists but nothing was ever seeded (the process died between
    # the two writes). the next open finds no marker and presents.
    cyc = _committed_cycle(env)
    RoomStore().open_cycle_room(U1, "cycle_retro", f"c{cyc['id']}")
    doors = CycleRooms(scorer=_scorer())
    result = _run(doors.open_retro(U1))
    assert result["created"] is False
    rows = _messages(result["room"]["id"])
    assert len(rows) == 1
    assert "execution 0.75" in rows[0].content
    assert "opens the ledger" in rows[0].content        # first-visit frame


def test_completeness_is_never_waived_at_the_door(env):
    # an active cycle - however overdue - has no seal, so there is no door
    _ended_cycle(status="active", ended_days_ago=5)
    doors = CycleRooms(scorer=_scorer())
    assert doors.doors(U1) is None
    assert _run(doors.open_retro(U1)) is None


def test_planning_follows_the_sealed_cycle_and_seeds_nothing(env):
    cyc = _committed_cycle(env)
    doors = CycleRooms(scorer=_scorer())
    result = _run(doors.open_planning(U1))
    assert result["created"] is True
    assert result["cycle"]["id"] == cyc["id"]
    assert _messages(result["room"]["id"]) == []


def test_the_doors_read_model_reports_rooms_and_the_card(env):
    _committed_cycle(env)
    doors = CycleRooms(scorer=_scorer())
    before = doors.doors(U1)
    assert before["scored"] is False
    assert before["retro"] is None and before["planning"] is None
    _run(doors.open_retro(U1))
    after = doors.doors(U1)
    assert after["scored"] is True
    assert after["retro"]["status"] == "open"
    assert after["planning"] is None


# --- hydration ---------------------------------------------------------------


def test_planning_hydrates_the_retro_it_follows(env):
    cyc = _committed_cycle(env)
    doors = CycleRooms(scorer=_scorer())
    _run(doors.open_retro(U1))
    planning = _run(doors.open_planning(U1))    # closes the retro -> summary
    ctx = ChordialContext(user_manager=None)
    ambient = ctx._compose_ambient(U1, stream_id=planning["room"]["id"])
    assert "the planning that follows cycle 'rewillow v1'" in ambient
    assert "the retrospective settled:" in ambient
    assert "the scorecard, as filed:" in ambient
    assert "execution 0.75" in ambient
    assert f"c{cyc['id']}" in ambient


def test_the_retro_ambient_orients_without_repeating_the_card(env):
    # the presentation is IN the transcript; ambient only orients
    _committed_cycle(env)
    doors = CycleRooms(scorer=_scorer())
    retro = _run(doors.open_retro(U1))
    ctx = ChordialContext(user_manager=None)
    ambient = ctx._compose_ambient(U1, stream_id=retro["room"]["id"])
    assert "retrospective for cycle 'rewillow v1'" in ambient
    assert "execution" not in ambient


def test_a_daily_room_never_hydrates_a_retro_summary(env):
    store = RoomStore()
    daily = store.current_room(U1, TODAY)
    retro, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    store.close_room(retro["id"])
    ctx = ChordialContext(user_manager=None)
    ambient = ctx._compose_ambient(U1, stream_id=daily["room_uuid"])
    assert ambient is None or "retrospective" not in ambient


def test_a_cardless_retro_briefing_never_claims_a_card(env):
    # the briefing must match the transcript: when no card filed, the
    # model is told there is NO scorecard - not that edwin presented one
    _committed_cycle(env)
    retro = _run(CycleRooms(scorer=None).open_retro(U1))
    ctx = ChordialContext(user_manager=None)
    ambient = ctx._compose_ambient(U1, stream_id=retro["room"]["id"])
    assert "NO scorecard has been filed" in ambient
    assert "edwin presented" not in ambient
    assert "on the table" not in ambient


def test_a_cardless_planning_briefing_never_claims_a_card(env):
    _committed_cycle(env)
    planning = _run(CycleRooms(scorer=None).open_planning(U1))
    ctx = ChordialContext(user_manager=None)
    ambient = ctx._compose_ambient(U1, stream_id=planning["room"]["id"])
    assert "NO scorecard has been filed" in ambient
    assert "as filed" not in ambient


# --- the director ------------------------------------------------------------


class _AllActive:
    """helper-state fake: everyone on the council is active."""

    def __init__(self, ids):
        self.ids = ids

    async def active_helpers(self, user_uuid):
        return [SimpleNamespace(helper_id=h, is_active=True)
                for h in self.ids]


def test_cycle_rooms_cast_the_cycle_family(env):
    from dainframe.core import Stimulus

    cast = ["vel", "pip", "skip", "remy", "mabel", "juniper", "edwin"]

    async def room_type_of(stream_id):
        return "cycle_retro"

    director = ChordialDirector(
        cast, helper_state_manager=_AllActive(cast),
        room_type_of=room_type_of)
    stimulus = Stimulus(
        kind="user_message", stream_id="retro-room",
        content="so how did the cycle actually go?",
        platform="app", scope="group", extras={"user_id": U1})
    candidates = _run(director._candidates(stimulus, set(cast)))
    assert candidates == ["vel", "edwin", "mabel", "pip"]


def test_planning_rooms_cast_the_same_family(env):
    from dainframe.core import Stimulus

    cast = ["vel", "pip", "skip", "remy", "mabel", "juniper", "edwin"]

    async def room_type_of(stream_id):
        return "cycle_planning"

    director = ChordialDirector(
        cast, helper_state_manager=_AllActive(cast),
        room_type_of=room_type_of)
    stimulus = Stimulus(
        kind="user_message", stream_id="planning-room",
        content="let's shape the next two weeks",
        platform="app", scope="group", extras={"user_id": U1})
    candidates = _run(director._candidates(stimulus, set(cast)))
    assert "skip" not in candidates and "juniper" not in candidates
    assert candidates[0] == "vel"


# --- room-bound turns --------------------------------------------------------


PUID = "app-device-user"


class CapturingOrchestrator:
    def __init__(self):
        self.stimuli = []

    async def handle(self, stimulus):
        self.stimuli.append(stimulus)
        return SimpleNamespace(any_delivered=True, refused=False,
                               error=None, reply=None, replies=())


async def _known_user():
    from src.managers.helper_state_manager import HelperStateManager
    user_uuid, _ = await UserManager().get_or_create_user("app", PUID)
    await UserManager().update_user_preferences(
        user_uuid, {"preferred_name": "megan"})
    await HelperStateManager().set_status(user_uuid, "vel", "active")
    return user_uuid


def _bound_message(room_uuid, content="the estimates were off again"):
    return UnifiedMessage(
        content=content, platform_user_id=PUID, platform="app",
        platform_message_id="m1", chat_scope="group", group_chat_id=PUID,
        room_uuid=room_uuid)


def test_a_room_bound_turn_lands_in_that_rooms_stream(env):
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    user_uuid = _run(_known_user())
    retro, _ = RoomStore().open_cycle_room(user_uuid, "cycle_retro", "c1")

    _run(svc.process_message(_bound_message(retro["room_uuid"])))

    stim = orch.stimuli[0]
    assert stim.stream_id == retro["room_uuid"]
    assert stim.scope == "group"


def test_a_turn_bound_to_a_closed_room_refuses_honestly(env):
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    user_uuid = _run(_known_user())
    store = RoomStore()
    retro, _ = store.open_cycle_room(user_uuid, "cycle_retro", "c1")
    store.close_room(retro["id"])

    reply = _run(svc.process_message(_bound_message(retro["room_uuid"])))

    assert reply == CLOSED_ROOM_REPLY
    assert orch.stimuli == []               # no turn ran, nothing rerouted


def test_a_turn_bound_to_someone_elses_room_refuses(env):
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    _run(_known_user())
    theirs, _ = RoomStore().open_cycle_room(U2, "cycle_retro", "c1")

    reply = _run(svc.process_message(_bound_message(theirs["room_uuid"])))

    assert reply == CLOSED_ROOM_REPLY
    assert orch.stimuli == []


def test_a_room_mid_close_refuses_turns(env):
    # 'closing' means the digest is already compressing: a turn slipping
    # in now would be missing from the summary the next room hydrates
    from src.database.models import Room
    orch = CapturingOrchestrator()
    svc = ChatService(orchestrator=orch)
    user_uuid = _run(_known_user())
    retro, _ = RoomStore().open_cycle_room(user_uuid, "cycle_retro", "c1")
    with db_mod.SessionLocal() as s:
        s.query(Room).filter(Room.room_uuid == retro["room_uuid"]).update(
            {"status": "closing"})
        s.commit()

    reply = _run(svc.process_message(_bound_message(retro["room_uuid"])))

    assert reply == CLOSED_ROOM_REPLY
    assert orch.stimuli == []


# --- the api doors -----------------------------------------------------------


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


def test_the_doors_endpoint_requires_a_device_token(env):
    async def flow(client):
        assert (await client.get("/api/v1/rooms/cycle")).status == 401
        assert (await client.post("/api/v1/rooms/cycle/retro")).status == 401
    _run(_with_service(flow))


def test_the_doors_endpoint_reports_the_sealed_cycle(env):
    cyc = _committed_cycle(env)
    token = _device_token()

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/rooms/cycle", headers=headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["doors"]["cycle"]["public_id"] == f"c{cyc['id']}"
        assert body["doors"]["scored"] is False
        assert body["doors"]["retro"] is None
    _run(_with_service(flow, cycle_rooms=CycleRooms(scorer=_scorer())))


def test_the_retro_endpoint_opens_scores_and_presents(env):
    _committed_cycle(env)
    token = _device_token()

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post("/api/v1/rooms/cycle/retro", headers=headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["created"] is True
        room_uuid = body["room"]["id"]
        log = await client.get(f"/api/v1/rooms/{room_uuid}/messages",
                               headers=headers)
        messages = (await log.json())["messages"]
        assert len(messages) == 1
        assert messages[0]["author"] == "edwin"
        assert "execution 0.75" in messages[0]["content"]
    _run(_with_service(flow, cycle_rooms=CycleRooms(scorer=_scorer())))


def test_the_retro_endpoint_404s_with_nothing_to_look_back_on(env):
    token = _device_token()

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post("/api/v1/rooms/cycle/retro", headers=headers)
        assert resp.status == 404
    _run(_with_service(flow, cycle_rooms=CycleRooms(scorer=_scorer())))


def test_sends_into_foreign_or_closed_rooms_refuse(env):
    from src.database.models import Room
    store = RoomStore()
    mine, _ = store.open_cycle_room(U1, "cycle_retro", "c1")
    store.close_room(mine["id"])
    theirs, _ = store.open_cycle_room(U2, "cycle_retro", "c1")
    mid_close, _ = store.open_cycle_room(U1, "cycle_planning", "c1")
    with db_mod.SessionLocal() as s:
        s.query(Room).filter(
            Room.room_uuid == mid_close["room_uuid"]).update(
            {"status": "closing"})
        s.commit()
    token = _device_token()

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        body = {"text": "hello?", "client_message_id": str(uuid_mod.uuid4())}
        closed = await client.post(
            f"/api/v1/rooms/{mine['room_uuid']}/messages",
            headers=headers, json=body)
        assert closed.status == 409
        closing = await client.post(
            f"/api/v1/rooms/{mid_close['room_uuid']}/messages",
            headers=headers, json=body)
        assert closing.status == 409         # mid-digest is not open
        foreign = await client.post(
            f"/api/v1/rooms/{theirs['room_uuid']}/messages",
            headers=headers, json=body)
        assert foreign.status == 404         # same answer as never-existed
    _run(_with_service(flow))


# --- the renderer ------------------------------------------------------------


def test_the_render_carries_no_overall_grade_and_owns_its_gaps(env):
    card = {
        "summary": "a steady cycle.",
        "detail": {
            "components": {
                "execution": {"score": 0.75, "evidence": ["3.0 of 4"]},
                "prioritization": {"score": None, "evidence": ["unfrozen"]},
                "estimation": {"score": 1.0, "evidence": []},
                "consistency": {"score": 0.5, "evidence": []},
                "sustainability": {"score": None, "evidence": []},
            },
            "findings": [{"claim": "the estimate held",
                          "suggested_action": "keep the block size"}],
        },
    }
    text = render_assessment(card)
    assert "execution 0.75 · 3.0 of 4" in text
    assert "prioritization —" in text
    assert "overall" not in text and "grade" not in text
    assert "- the estimate held -> keep the block size" in text
