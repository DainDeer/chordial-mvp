import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.database import get_db
from src.database.models import AgentTrace, Base, UsageLog
from src.providers.ai.types import Usage
from src.services.usage_recorder import UsageRecorder


@pytest.fixture()
def db(monkeypatch):
    _fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", test_session)
    yield test_session
    engine.dispose()


def test_recorder_persists_explicit_and_utility_helper_ids(db):
    recorder = UsageRecorder()
    recorder.record_call(
        user_uuid=None,
        platform="telegram",
        provider="fake",
        model="model",
        role="conversation",
        usage=Usage(input_tokens=2),
        helper_id="aria",
    )
    recorder.record_call(
        user_uuid=None,
        platform=None,
        provider="fake",
        model="utility",
        role="reconciler",
        usage=Usage(input_tokens=1),
    )
    recorder.record_trace(
        user_uuid=None,
        platform=None,
        turn_kind="curation",
        iterations=1,
        hit_iteration_cap=False,
        tool_trace=[],
        final_text_length=0,
        stop_reason=None,
        total_usage=Usage(output_tokens=3),
    )

    with get_db() as session:
        calls = session.query(UsageLog).order_by(UsageLog.id.desc()).limit(2).all()
        trace = session.query(AgentTrace).order_by(AgentTrace.id.desc()).first()

        assert [row.helper_id for row in reversed(calls)] == ["aria", "reconciler"]
        assert trace.helper_id == "curator"
