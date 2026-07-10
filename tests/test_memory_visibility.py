"""tests for the v3 shared-pool-plus-privates memory model (docs/V3_DESIGN.md
sections 2 and 6): Memory.created_by/visibility, helper-scoped reads, the
curator's cross-scope merge guard, and the HelperState uniqueness constraint.

isolated temp DB (patches the SessionLocal get_db reads), plain-asyncio style -
mirrors tests/test_memory_upsert.py and tests/test_memory_curator.py.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, Memory, HelperState  # noqa: E402
from src.managers.memories_manager import (  # noqa: E402
    MemoriesManager, MemoryType, MemorySource,
)
from src.services.memory_curator import MemoryCuratorService  # noqa: E402
from src.providers.ai.types import AIResponse, ChatTurn, Usage  # noqa: E402
from src.utils.timezone_utils import utc_now  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeProvider:
    model = "fake-utility"

    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.calls = 0

    async def create_message(self, request):
        self.calls += 1
        return AIResponse(
            text=self.reply_text, tool_calls=[], stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
            assistant_turn=ChatTurn(role="assistant", content=self.reply_text),
        )


class NoUsage:
    def record_call(self, **k): pass
    def record_trace(self, **k): pass


def _curator(reply_text):
    return MemoryCuratorService(FakeProvider(reply_text), "fake", usage_recorder=NoUsage())


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


def _save(mgr, instruction, keywords, mtype=MemoryType.EPISODIC, core=False,
          created_by=None, visibility=None):
    kwargs = dict(
        user_uuid="u1", ai_instruction=instruction, memory_type=mtype,
        source=MemorySource.AI_INFERRED, keywords=keywords, core=core,
    )
    if created_by is not None:
        kwargs["created_by"] = created_by
    if visibility is not None:
        kwargs["visibility"] = visibility
    return run(mgr.upsert_memory(**kwargs))


def _add_memory(db, *, instruction, keywords="", mtype="EPISODIC", core=False,
                weight=1.0, curated=False, created_by="chordial", visibility="shared"):
    with db() as s:
        m = Memory(
            user_uuid="u1", ai_instruction=instruction, keywords=keywords,
            memory_type=mtype, source="AI_INFERRED", core=core, weighting=weight,
            curated_at=(utc_now() if curated else None),
            created_by=created_by, visibility=visibility,
        )
        s.add(m)
        s.commit()
        return m.id


def _memory(db, mid):
    with db() as s:
        return s.query(Memory).filter(Memory.id == mid).first()


def _all(db):
    with db() as s:
        return s.query(Memory).filter(Memory.is_active == True).all()


# --- defaults on save -------------------------------------------------------

def test_upsert_defaults_to_chordial_shared(db):
    mgr = MemoriesManager()
    res = _save(mgr, "Dain likes tea", ["tea"])
    assert res.action == "inserted"
    row = _memory(db, res.memory_id)
    assert row.created_by == "chordial"
    assert row.visibility == "shared"


def test_create_memory_defaults_to_chordial_shared(db):
    mgr = MemoriesManager()
    run(mgr.create_memory(
        user_uuid="u1", ai_instruction="Dain is a pink nerd deer",
        memory_type=MemoryType.FACT, source=MemorySource.USER_EXPLICIT, core=True,
    ))
    with db() as s:
        row = s.query(Memory).filter(Memory.user_uuid == "u1").one()
    assert row.created_by == "chordial"
    assert row.visibility == "shared"


def test_upsert_honors_explicit_created_by_and_visibility(db):
    mgr = MemoriesManager()
    res = _save(mgr, "inside joke about capybaras", ["capybara"],
                created_by="mochi", visibility="private")
    row = _memory(db, res.memory_id)
    assert row.created_by == "mochi"
    assert row.visibility == "private"


def test_invalid_visibility_rejected(db):
    mgr = MemoriesManager()
    with pytest.raises(ValueError):
        _save(mgr, "Dain likes tea", ["tea"], visibility="secret")
    with pytest.raises(ValueError):
        run(mgr.create_memory(
            user_uuid="u1", ai_instruction="x", memory_type=MemoryType.FACT,
            source=MemorySource.USER_EXPLICIT, visibility="secret",
        ))


# --- reinforcement is scoped to visibility/helper ---------------------------

def test_reinforcement_scoped_to_same_visibility_and_helper(db):
    """a private memory only reinforces another private memory from the SAME
    helper - it never merges into a shared row or a sibling's private one."""
    mgr = MemoriesManager()
    _save(mgr, "Dain likes tea", ["tea", "drink"], created_by="tempo", visibility="private")
    # same text, different helper's private scope -> separate row, not reinforced
    res_other_helper = _save(mgr, "Dain likes tea", ["tea", "drink"],
                              created_by="aria", visibility="private")
    # same text, shared scope -> also separate row
    res_shared = _save(mgr, "Dain likes tea", ["tea", "drink"],
                        created_by="chordial", visibility="shared")

    assert res_other_helper.action == "inserted"
    assert res_shared.action == "inserted"
    assert len(_all(db)) == 3

    # but a second save from tempo, private, DOES reinforce the first
    res_same_helper = _save(mgr, "Dain likes tea", ["tea", "drink"],
                             created_by="tempo", visibility="private")
    assert res_same_helper.action == "reinforced"
    assert len(_all(db)) == 3


# --- helper-scoped reads -----------------------------------------------------

def test_get_active_memories_filters_by_helper(db):
    mgr = MemoriesManager()
    _add_memory(db, instruction="shared fact", created_by="chordial", visibility="shared")
    _add_memory(db, instruction="tempo private note", created_by="tempo", visibility="private")
    _add_memory(db, instruction="aria private note", created_by="aria", visibility="private")

    tempo_view = run(mgr.get_active_memories(user_uuid="u1", helper_id="tempo"))
    tempo_texts = {m.ai_instruction for m in tempo_view}
    assert tempo_texts == {"shared fact", "tempo private note"}

    aria_view = run(mgr.get_active_memories(user_uuid="u1", helper_id="aria"))
    aria_texts = {m.ai_instruction for m in aria_view}
    assert aria_texts == {"shared fact", "aria private note"}


def test_get_active_memories_helper_id_none_returns_everything(db):
    mgr = MemoriesManager()
    _add_memory(db, instruction="shared fact", created_by="chordial", visibility="shared")
    _add_memory(db, instruction="tempo private note", created_by="tempo", visibility="private")

    everything = run(mgr.get_active_memories(user_uuid="u1"))
    assert {m.ai_instruction for m in everything} == {"shared fact", "tempo private note"}


def test_search_memories_by_keywords_respects_helper_filter(db):
    mgr = MemoriesManager()
    _add_memory(db, instruction="shared fact", keywords="alpha", created_by="chordial", visibility="shared")
    _add_memory(db, instruction="tempo private note", keywords="alpha", created_by="tempo", visibility="private")

    aria_matches = run(mgr.search_memories_by_keywords("u1", ["alpha"], helper_id="aria"))
    assert {m.ai_instruction for m in aria_matches} == {"shared fact"}

    tempo_matches = run(mgr.search_memories_by_keywords("u1", ["alpha"], helper_id="tempo"))
    assert {m.ai_instruction for m in tempo_matches} == {"shared fact", "tempo private note"}

    unfiltered = run(mgr.search_memories_by_keywords("u1", ["alpha"]))
    assert {m.ai_instruction for m in unfiltered} == {"shared fact", "tempo private note"}


def test_core_memories_for_prompt_respects_helper_filter_and_includes_created_by(db):
    mgr = MemoriesManager()
    _add_memory(db, instruction="identity: chordial is ember", core=True,
                created_by="chordial", visibility="shared")
    _add_memory(db, instruction="tempo private identity note", core=True,
                created_by="tempo", visibility="private")

    aria_core = run(mgr.get_core_memories_for_prompt("u1", helper_id="aria"))
    assert [c["instruction"] for c in aria_core] == ["identity: chordial is ember"]
    assert all("created_by" in c for c in aria_core)
    assert aria_core[0]["created_by"] == "chordial"

    tempo_core = run(mgr.get_core_memories_for_prompt("u1", helper_id="tempo"))
    assert {c["instruction"] for c in tempo_core} == {
        "identity: chordial is ember", "tempo private identity note",
    }

    unfiltered = run(mgr.get_core_memories_for_prompt("u1"))
    assert len(unfiltered) == 2


# --- curator: scope carries over, cross-scope merges are rejected -----------

def test_curator_merge_preserves_created_by_and_visibility(db):
    a = _add_memory(db, instruction="tempo notes dain likes morning runs",
                    keywords="running", created_by="tempo", visibility="private")
    b = _add_memory(db, instruction="tempo notes dain runs in the morning",
                    keywords="running,morning", created_by="tempo", visibility="private")
    reply = '{"operations": [{"op": "merge", "canonical_id": %d, "absorb_ids": [%d]}]}' % (a, b)

    result = run(_curator(reply).curate_user("u1"))

    assert len(result.applied) == 1 and not result.rejected
    canonical = _memory(db, a)
    assert canonical.created_by == "tempo"
    assert canonical.visibility == "private"


def test_curator_merge_rejects_private_absorbing_into_shared(db):
    shared = _add_memory(db, instruction="dain likes morning runs",
                         created_by="chordial", visibility="shared")
    private = _add_memory(db, instruction="dain likes running with tempo",
                          created_by="tempo", visibility="private")
    reply = ('{"operations": [{"op": "merge", "canonical_id": %d, "absorb_ids": [%d]}]}'
              % (shared, private))

    result = run(_curator(reply).curate_user("u1"))

    assert not result.applied
    assert len(result.rejected) == 1
    assert "scope mismatch" in result.rejected[0]["reason"]
    # both rows untouched
    assert _memory(db, shared).is_active is True
    assert _memory(db, private).is_active is True
    assert _memory(db, private).merged_into is None


def test_curator_merge_rejects_private_rows_from_different_helpers(db):
    tempo_private = _add_memory(db, instruction="tempo's private note about pacing",
                                created_by="tempo", visibility="private")
    aria_private = _add_memory(db, instruction="aria's private note about pacing",
                               created_by="aria", visibility="private")
    reply = ('{"operations": [{"op": "merge", "canonical_id": %d, "absorb_ids": [%d]}]}'
              % (tempo_private, aria_private))

    result = run(_curator(reply).curate_user("u1"))

    assert not result.applied
    assert len(result.rejected) == 1
    assert "scope mismatch" in result.rejected[0]["reason"]


# --- HelperState unique constraint ------------------------------------------

def test_helper_state_unique_constraint(db):
    with db() as s:
        s.add(HelperState(user_uuid="u1", helper_id="tempo", status="active"))
        s.commit()

    with db() as s:
        s.add(HelperState(user_uuid="u1", helper_id="tempo", status="not_met"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_helper_state_allows_same_helper_for_different_users(db):
    with db() as s:
        s.add(User(uuid="u2", preferred_name="other"))
        s.add(HelperState(user_uuid="u1", helper_id="tempo", status="active"))
        s.add(HelperState(user_uuid="u2", helper_id="tempo", status="not_met"))
        s.commit()  # no error - different users

    with db() as s:
        rows = s.query(HelperState).filter(HelperState.helper_id == "tempo").all()
        assert len(rows) == 2
