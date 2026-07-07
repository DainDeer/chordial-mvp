"""upsert/reinforcement tests for MemoriesManager.

the concrete case these lock down is the live one: two saves of the same
"degen hours" experiment should end up as ONE reinforced row, not two. isolated
temp DB (patches the SessionLocal get_db reads), plain-asyncio style.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, Memory  # noqa: E402
from src.managers.memories_manager import (  # noqa: E402
    MemoriesManager, MemoryType, MemorySource,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain"))
        s.commit()
    yield TestSession
    engine.dispose()


def _save(mgr, instruction, keywords, mtype=MemoryType.EPISODIC, core=False):
    return run(mgr.upsert_memory(
        user_uuid="u1",
        ai_instruction=instruction,
        memory_type=mtype,
        source=MemorySource.AI_INFERRED,
        keywords=keywords,
        core=core,
    ))


def _active(db):
    with db() as s:
        return s.query(Memory).filter(Memory.is_active == True).all()


def test_first_save_inserts(db):
    mgr = MemoriesManager()
    res = _save(mgr, "Dain is trying 'degen hours' this week", ["degen hours", "sleep"])
    assert res.action == "inserted"
    assert res.times_seen == 1
    assert len(_active(db)) == 1


def test_near_duplicate_reinforces_instead_of_inserting(db):
    """the live degen-hours case: second, slightly-reworded save of the same
    fact reinforces the first row rather than adding a second."""
    mgr = MemoriesManager()
    _save(mgr, "Dain is experimenting with 'degen hours' (late-night schedule) this week to track productivity",
          ["degen hours", "sleep schedule", "productivity", "night owl"])
    res2 = _save(mgr, "Dain is experimenting this week with 'degen hours' (staying up late) to see if productivity improves",
                 ["degen hours", "sprint planning", "productivity experiment", "night owl"])

    rows = _active(db)
    assert len(rows) == 1                     # still one memory
    assert res2.action == "reinforced"
    assert res2.times_seen == 2
    assert rows[0].weighting == 2.0           # bumped from 1.0
    assert rows[0].reinforced_count == 1
    # keywords are unioned across both saves
    kw = set(rows[0].keywords.split(","))
    assert {"degen hours", "night owl", "productivity", "sprint planning"} <= kw
    # reinforcement flags the row for re-curation
    assert rows[0].curated_at is None


def test_unrelated_memory_inserts_separately(db):
    mgr = MemoriesManager()
    _save(mgr, "Dain is trying 'degen hours' this week", ["degen hours", "sleep"])
    res = _save(mgr, "Dain wants chordial to have a red panda mascot someday",
                ["red panda", "mascot", "character"], mtype=MemoryType.PREFERENCE)
    assert res.action == "inserted"
    assert len(_active(db)) == 2


def test_weight_caps_at_ten(db):
    mgr = MemoriesManager()
    for _ in range(15):
        _save(mgr, "Dain drinks a lot of coffee", ["coffee", "caffeine"])
    rows = _active(db)
    assert len(rows) == 1
    assert rows[0].weighting == 10.0          # capped, not 15


def test_core_memories_are_never_deduped(db):
    """core memories are deliberate identity facts - always insert verbatim."""
    mgr = MemoriesManager()
    _save(mgr, "Dain is a pink nerd deer building chordial", ["identity", "core"],
          mtype=MemoryType.FACT, core=True)
    res = _save(mgr, "Dain is a pink nerd deer building chordial", ["identity", "core"],
                mtype=MemoryType.FACT, core=True)
    assert res.action == "inserted"
    assert len(_active(db)) == 2
    assert all(m.core and m.weighting == 999.0 for m in _active(db))


def test_reinforcement_is_scoped_to_same_type(db):
    """same text but different memory_type shouldn't collapse together."""
    mgr = MemoriesManager()
    _save(mgr, "Dain likes tea", ["tea", "drink"], mtype=MemoryType.FACT)
    res = _save(mgr, "Dain likes tea", ["tea", "drink"], mtype=MemoryType.PREFERENCE)
    assert res.action == "inserted"
    assert len(_active(db)) == 2
