"""chordial's SqlEventStore runs the dainframe EventStoreContract - the exact
conformance suite every adapter must pass (the-dainframe DESIGN.md §8):
ordering, unique immutable ids, visibility-before-windowing, message-count
windows that carry riders, latest-query behavior.

plus adapter-specific round-trip checks: scope/audience map onto chordial's
`event_metadata` convention (scope absent = group; with_helper = audience), so
rows written through the adapter read identically through chordial's EventLog.
"""
import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User  # noqa: E402
from src.managers.event_log import EventLog  # noqa: E402
from src.managers.event_store_adapter import SqlEventStore  # noqa: E402

from dainframe.core.events import EventQuery, NewEvent  # noqa: E402
from dainframe.testing import EventStoreContract  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TestSqlEventStore(EventStoreContract):
    """the library conformance suite, against the real SQL adapter."""

    def setup_method(self):
        self._fd, self._path = tempfile.mkstemp(suffix="_store_test.db")
        self._engine = create_engine(
            f"sqlite:///{self._path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=self._engine)
        self._sessions = sessionmaker(
            autocommit=False, autoflush=False, bind=self._engine
        )
        self._old_session_local = db_mod.SessionLocal
        db_mod.SessionLocal = self._sessions

    def teardown_method(self):
        db_mod.SessionLocal = self._old_session_local
        self._engine.dispose()

    def make_store(self, visibility=None):
        stream_id = f"stream-{uuid.uuid4().hex[:8]}"
        with self._sessions() as s:
            s.add(User(uuid=stream_id, preferred_name="tester"))
            s.commit()
        return SqlEventStore(stream_id, visibility=visibility)

    # --- adapter-specific: the chordial metadata convention round-trips ------

    def test_scope_audience_round_trip_through_chordial_event_log(self):
        """a dm event appended through the adapter reads back through
        chordial's OWN EventLog with the same privacy semantics - the two
        views agree on what's private to whom."""
        store = self.make_store()
        run(store.append(NewEvent(
            author_type="user", author="user", kind="message",
            content="a private aside", message_type="conversation",
            scope="dm", audience="aria",
        )))
        run(store.append(NewEvent(
            author_type="agent", author="chordial", kind="message",
            content="a group line", message_type="conversation",
        )))

        # dainframe view round-trips
        events = run(store.read(EventQuery()))
        assert (events[0].scope, events[0].audience) == ("dm", "aria")
        assert (events[1].scope, events[1].audience) == ("group", None)

        # chordial's EventLog applies its dm privacy rule to the same rows
        log = EventLog(store.stream_id)
        assert [e.content for e in log.recent(visible_to="aria")] == [
            "a private aside", "a group line",
        ]
        assert [e.content for e in log.recent(visible_to="tempo")] == [
            "a group line",
        ]

    def test_stale_metadata_cannot_override_canonical_scope_audience(self):
        """the canonical NewEvent fields win: reserved scope/with_helper keys
        in caller metadata are stripped before the convention is applied, so
        metadata can never smuggle an event into a different privacy channel."""
        store = self.make_store()
        stored = run(store.append(NewEvent(
            author_type="user", author="user", kind="message",
            content="a group message", message_type="conversation",
            scope="group", audience=None,
            metadata={"scope": "dm", "with_helper": "aria", "other": "kept"},
        )))
        assert stored.scope == "group"
        assert stored.audience is None
        assert "with_helper" not in stored.metadata
        assert stored.metadata.get("other") == "kept"     # honest keys survive

        # and chordial's EventLog agrees: every helper can see it
        log = EventLog(store.stream_id)
        assert [e.content for e in log.recent(visible_to="tempo")] == [
            "a group message",
        ]

    def test_group_events_write_no_scope_tag(self):
        """the cache-stability convention: a group event's metadata carries NO
        scope tag (absence means group), byte-identical to pre-dm history."""
        store = self.make_store()
        stored = run(store.append(NewEvent(
            author_type="user", author="user", kind="message",
            content="hello", message_type="conversation",
        )))
        assert "scope" not in stored.metadata
        assert stored.scope == "group"     # ...but the mapped view says group
