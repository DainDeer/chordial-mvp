"""observations (phase 3b): the council's structured noticing.

the invariants: kind is a closed vocabulary; attribution (which helper,
which user, which room) comes from the tool-loop context, never model
input; observations are queryable per user/helper/kind; and the memory /
observation boundary is carried in the tool description (facts vs
noticing) so the two stores don't blur.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, User
from dainframe.tools.context import ToolContext
from src.services.observations import ObservationStore
from src.services.tools.observation_tools import (
    LIST_OBSERVATIONS,
    RECORD_OBSERVATION,
)

U1 = "user-one"


def run(coro):
    return asyncio.run(coro)


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


def _ctx(actor="pip", stream="room-1"):
    return ToolContext(stream_id=stream, activation_id="test-act",
                       actor=actor, metadata={"user_id": U1})


# --- the store ---------------------------------------------------------------


def test_store_roundtrip_and_filters(env):
    store = ObservationStore()
    store.record(U1, "pip", "pattern", "mornings are her best focus blocks",
                 room_uuid="room-1")
    store.record(U1, "pip", "progress", "three cycles of steady task completion")
    store.record(U1, "mabel", "concern", "sleep debt building this week",
                 evidence={"note": "mentioned 2am bedtimes twice"})

    all_rows = store.recent(U1)
    assert [r["kind"] for r in all_rows] == ["concern", "progress", "pattern"]

    pips = store.recent(U1, helper_id="pip")
    assert {r["helper_id"] for r in pips} == {"pip"}

    concerns = store.recent(U1, kind="concern")
    assert len(concerns) == 1
    assert concerns[0]["evidence"]["note"].startswith("mentioned")


def test_store_enforces_kind_and_content(env):
    store = ObservationStore()
    with pytest.raises(ValueError, match="unknown observation kind"):
        store.record(U1, "pip", "vibe", "feels off")
    with pytest.raises(ValueError, match="needs content"):
        store.record(U1, "pip", "pattern", "   ")


# --- the tools ---------------------------------------------------------------


def test_record_observation_attributes_from_context_not_input(env):
    result = run(RECORD_OBSERVATION.handler(
        {"kind": "pattern",
         "content": "music practice planned on meeting days never survives",
         "evidence": "third week running"},
        _ctx(actor="juniper", stream="room-42"),
    ))
    assert "observed (pattern)" in result

    row = ObservationStore().recent(U1)[0]
    assert row["helper_id"] == "juniper"   # the acting helper, not model input
    assert row["user_uuid"] == U1          # the identity split's metadata
    assert row["room_uuid"] == "room-42"   # the stream this turn ran in
    assert row["evidence"] == {"note": "third week running"}


def test_record_observation_translates_store_errors(env):
    # invalid kind comes back as a corrective string, never a raise
    result = run(RECORD_OBSERVATION.handler(
        {"kind": "vibes", "content": "something"}, _ctx()))
    assert "unknown observation kind" in result


def test_list_observations_reads_oldest_first_with_mine_filter(env):
    store = ObservationStore()
    store.record(U1, "pip", "progress", "first win")
    store.record(U1, "skip", "pattern", "walks happen after lunch")

    everyone = run(LIST_OBSERVATIONS.handler({}, _ctx(actor="pip")))
    assert everyone.index("first win") < everyone.index("walks happen")

    mine = run(LIST_OBSERVATIONS.handler({"helper": "mine"}, _ctx(actor="pip")))
    assert "first win" in mine and "walks happen" not in mine

    empty = run(LIST_OBSERVATIONS.handler({"kind": "concern"}, _ctx()))
    assert "no observations" in empty


def test_tool_flags_and_the_memory_boundary():
    """record = terminal side effect that lands in the transcript (the
    save_memory precedent); list = pure read. and the description must keep
    drawing the memory/observation line - it's the only thing stopping a
    model from double-writing."""
    assert RECORD_OBSERVATION.terminal is True
    assert RECORD_OBSERVATION.record_event is True
    assert LIST_OBSERVATIONS.record_event is False
    description = RECORD_OBSERVATION.definition.description
    assert "NOT save_memory" in description


def test_observation_tools_ride_every_council_card():
    from src.personas import load_personas
    for card in load_personas().values():
        if card.tools is None:
            continue  # the chair's full registry includes them
        assert "record_observation" in card.tools, card.id
        assert "list_observations" in card.tools, card.id
