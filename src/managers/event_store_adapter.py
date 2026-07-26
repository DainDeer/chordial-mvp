"""chordial's SQL implementation of the dainframe EventStore contract.

a REAL adapter, not a rename (the-dainframe DESIGN.md §3/§4.1): it maps
chordial's `conversation_events` rows to dainframe's shared Event vocabulary,
implements EventQuery with visibility-BEFORE-windowing and message-count
windows, and is proven by the library's EventStoreContract conformance suite
(tests/test_event_store_contract.py).

mapping notes:
- one adapter instance is one STREAM: chordial's stream id is the user_uuid.
- scope/audience ride in event_metadata exactly the way chordial's EventLog
  writes them (`scope` absent means group; `with_helper` is the audience), so
  rows written by either side read identically from both. the adapter does NOT
  replace EventLog yet - the live orchestrator path keeps EventLog until the
  phase-3 engine swap; this class is the contract-proven bridge that swap
  will stand on.
- methods are async per the protocol but delegate to chordial's synchronous
  SQLAlchemy session (§11.8: chordial adapts its sync store behind the async
  boundary during migration).
"""
from __future__ import annotations

from typing import List, Optional

from dainframe.core.events import Event, EventQuery, NewEvent, VisibilityPolicy

from src.database.database import get_db
from src.database.models import ConversationEvent
from src.managers.event_log import _scope_meta


def _to_dainframe(row: ConversationEvent) -> Event:
    meta = dict(row.event_metadata or {})
    scope = meta.get("scope", "group")       # absence means the shared channel
    audience = meta.get("with_helper")
    return Event(
        event_id=str(row.id),
        author_type=row.author_type,
        author=row.author,
        kind=row.kind,
        content=row.content,
        created_at=row.created_at,
        message_type=row.message_type,
        platform=row.platform,
        scope=scope,
        audience=audience,
        metadata=meta,
    )


class SqlEventStore:
    """dainframe EventStore over one chordial user's conversation_events."""

    def __init__(self, stream_id: str, visibility: Optional[VisibilityPolicy] = None):
        self.stream_id = stream_id
        self._visibility = visibility

    # --- writes --------------------------------------------------------------

    async def append(self, event: NewEvent) -> Event:
        meta = dict(event.metadata)
        meta.update(_scope_meta(event.scope or "group", event.audience))
        with get_db() as db:
            row = ConversationEvent(
                user_uuid=self.stream_id,
                platform=event.platform,
                author_type=event.author_type,
                author=event.author,
                kind=event.kind,
                content=event.content,
                message_type=event.message_type,
                event_metadata=meta,
            )
            db.add(row)
            db.flush()  # id + server defaults while the session is open
            stored = _to_dainframe(row)
            db.commit()
        return stored

    # --- reads ---------------------------------------------------------------

    async def read(self, query: EventQuery) -> List[Event]:
        filtered = self._filtered(query)
        if query.message_limit is None:
            return filtered
        # window on the last N MESSAGE events; non-message events inside that
        # id-ordered window ride along (the exact EventLog.recent semantic)
        message_positions = [i for i, e in enumerate(filtered) if e.kind == "message"]
        if not message_positions:
            return []
        window = message_positions[-query.message_limit:]
        return filtered[window[0]:]

    async def latest(self, query: EventQuery) -> Optional[Event]:
        filtered = self._filtered(query)
        return filtered[-1] if filtered else None

    def _filtered(self, query: EventQuery) -> List[Event]:
        with get_db() as db:
            rows = db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.stream_id,
            ).order_by(ConversationEvent.id).all()
            events = [_to_dainframe(r) for r in rows]
        # visibility filters BEFORE windowing - a limit means visible things
        if query.viewer is not None and self._visibility is not None:
            events = [e for e in events if self._visibility(e, query.viewer)]
        return [
            e
            for e in events
            if (query.kinds is None or e.kind in query.kinds)
            and (query.author_types is None or e.author_type in query.author_types)
            and (query.authors is None or e.author in query.authors)
            and (query.message_types is None or e.message_type in query.message_types)
        ]
