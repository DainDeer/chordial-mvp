"""memory-curator tests: parsing, and the validated executor.

the model only *proposes* operations - these lock down that the executor
verifies ids, protects core memories, sums/caps merged weights, and stamps
curated_at so a clean table isn't re-reviewed. faked provider, isolated temp DB.
"""
import asyncio
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, Memory  # noqa: E402
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


def _add_memory(db, *, instruction, keywords="", mtype="EPISODIC", core=False,
                weight=1.0, curated=False, age_minutes=60):
    with db() as s:
        m = Memory(
            user_uuid="u1", ai_instruction=instruction, keywords=keywords,
            memory_type=mtype, source="AI_INFERRED", core=core, weighting=weight,
            curated_at=(utc_now() if curated else None),
            created_at=utc_now() - timedelta(minutes=age_minutes),
        )
        s.add(m)
        s.commit()
        return m.id


def _curator(reply_text):
    return MemoryCuratorService(FakeProvider(reply_text), "fake", usage_recorder=NoUsage())


def _memory(db, mid):
    with db() as s:
        return s.query(Memory).filter(Memory.id == mid).first()


# --- parsing ---------------------------------------------------------------

def test_parse_handles_fences_and_prose():
    c = MemoryCuratorService.__new__(MemoryCuratorService)
    clean = '{"operations": [{"op": "expire", "id": 3}]}'
    fenced = "```json\n" + clean + "\n```"
    chatty = "sure! here you go:\n" + clean + "\nlet me know if you want more."
    for text in (clean, fenced, chatty):
        ops = MemoryCuratorService._parse_operations(text)
        assert ops == [{"op": "expire", "id": 3}]

    assert MemoryCuratorService._parse_operations("no json here") == []
    assert MemoryCuratorService._parse_operations("") == []


# --- executor: merge -------------------------------------------------------

def test_merge_keeps_canonical_absorbs_others_and_sums_weight(db):
    a = _add_memory(db, instruction="Dain trying degen hours this week",
                    keywords="degen hours,sleep", weight=2.0)
    b = _add_memory(db, instruction="Dain experimenting with degen hours",
                    keywords="degen hours,productivity", weight=1.0)
    reply = ('{"operations": [{"op": "merge", "canonical_id": %d, "absorb_ids": [%d],'
             ' "instruction": "Dain is trying degen hours this week to boost productivity",'
             ' "keywords": ["degen hours", "sleep", "productivity"]}]}') % (a, b)

    result = run(_curator(reply).curate_user("u1"))

    assert len(result.applied) == 1 and not result.rejected
    canonical, absorbed = _memory(db, a), _memory(db, b)
    assert canonical.is_active and canonical.weighting == 3.0        # 2 + 1
    assert canonical.ai_instruction.endswith("boost productivity")
    assert absorbed.is_active is False and absorbed.merged_into == a
    assert canonical.curated_at is not None


def test_merge_weight_is_capped(db):
    a = _add_memory(db, instruction="coffee lover fact one", weight=8.0)
    b = _add_memory(db, instruction="coffee lover fact two", weight=7.0)
    reply = '{"operations": [{"op": "merge", "canonical_id": %d, "absorb_ids": [%d]}]}' % (a, b)
    run(_curator(reply).curate_user("u1"))
    assert _memory(db, a).weighting == 10.0   # 15 capped


# --- executor: core protection --------------------------------------------

def test_core_memory_cannot_be_expired_or_absorbed(db):
    core = _add_memory(db, instruction="Dain is a pink nerd deer", core=True,
                       weight=999.0, mtype="FACT")
    other = _add_memory(db, instruction="Dain likes tea", mtype="FACT")
    reply = ('{"operations": ['
             '{"op": "expire", "id": %d},'
             '{"op": "merge", "canonical_id": %d, "absorb_ids": [%d]}]}') % (core, other, core)

    result = run(_curator(reply).curate_user("u1"))

    assert _memory(db, core).is_active is True        # untouched
    assert len(result.rejected) == 2 and not result.applied


# --- executor: bad ids -----------------------------------------------------

def test_operations_on_unknown_ids_are_rejected(db):
    real = _add_memory(db, instruction="Dain likes tea")
    reply = '{"operations": [{"op": "expire", "id": 999999}]}'
    result = run(_curator(reply).curate_user("u1"))
    assert _memory(db, real).is_active is True
    assert len(result.rejected) == 1 and not result.applied


# --- executor: promote & expire & update ----------------------------------

def test_promote_expire_update(db):
    p = _add_memory(db, instruction="Dain streams every friday", weight=3.0)
    e = _add_memory(db, instruction="Dain busy just this afternoon")
    u = _add_memory(db, instruction="Dain likes ttea", keywords="tea")
    reply = ('{"operations": ['
             '{"op": "promote", "id": %d},'
             '{"op": "expire", "id": %d},'
             '{"op": "update", "id": %d, "instruction": "Dain likes tea", "weight_delta": 1}]}'
             ) % (p, e, u)

    result = run(_curator(reply).curate_user("u1"))

    assert len(result.applied) == 3 and not result.rejected
    assert _memory(db, p).core is True and _memory(db, p).weighting == 999.0
    assert _memory(db, e).is_active is False
    assert _memory(db, u).ai_instruction == "Dain likes tea" and _memory(db, u).weighting == 2.0


# --- reviewed rows are stamped --------------------------------------------

def test_untouched_pending_rows_get_stamped(db):
    m = _add_memory(db, instruction="Dain likes tea")   # pending, curator does nothing
    run(_curator('{"operations": []}').curate_user("u1"))
    assert _memory(db, m).curated_at is not None         # won't be re-reviewed


def test_no_provider_call_when_nothing_pending(db):
    _add_memory(db, instruction="already reviewed", curated=True)
    curator = _curator('{"operations": []}')
    result = run(curator.curate_user("u1"))
    assert result.reviewed == 0
    assert curator.provider.calls == 0                   # skipped the api call


# --- discovery / debounce --------------------------------------------------

def test_find_users_respects_debounce_and_curated_flag(db):
    # settled pending memory (old) -> user is due
    _add_memory(db, instruction="settled", age_minutes=60)
    curator = _curator('{"operations": []}')
    assert run(curator.find_users_needing_curation()) == ["u1"]


def test_find_users_skips_recent_activity(db):
    # pending but just created -> still inside the debounce window
    _add_memory(db, instruction="fresh", age_minutes=1)
    curator = _curator('{"operations": []}')
    assert run(curator.find_users_needing_curation()) == []


def test_find_users_skips_fully_curated(db):
    _add_memory(db, instruction="done", curated=True, age_minutes=60)
    curator = _curator('{"operations": []}')
    assert run(curator.find_users_needing_curation()) == []
