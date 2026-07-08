"""the event log: one (user_uuid, platform) channel's ordered record of
everything that happened - user messages, agent replies, agent tool actions,
(future) system notes.

replaces the old ConversationManager/Conversation pair. deliberately db-backed
with NO in-memory message cache: once the orchestrator writes action events
alongside messages, a long-lived in-memory list is a cache-coherence bug
factory, and reading ~50 rows from local WAL sqlite per turn costs nothing.
sqlite is the cache.

ordering: single writer + sqlite autoincrement means id order IS insertion
order IS chronology. every read here orders by id, never created_at (which is
kept for humans and elapsed-time math).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
import logging

from src.database.database import get_db
from src.database.models import ConversationEvent
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """a detached, session-safe view of one conversation event."""
    author_type: str                 # 'user' | 'agent' | 'system'
    author: str                      # 'user' | 'chordial' | 'curator' | future personas
    kind: str                        # 'message' | 'action' | 'note'
    content: str
    created_at: datetime = field(default_factory=utc_now)
    message_type: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    db_id: Optional[int] = None

    @property
    def role(self) -> str:
        """the prompt-side view of the author: agents (and system) render on
        the assistant side of the wire, humans on the user side."""
        return "user" if self.author_type == "user" else "assistant"

    @classmethod
    def from_row(cls, row: ConversationEvent) -> "Event":
        return cls(
            author_type=row.author_type,
            author=row.author,
            kind=row.kind,
            content=row.content,
            created_at=row.created_at,
            message_type=row.message_type,
            metadata=dict(row.event_metadata or {}),
            db_id=row.id,
        )


class EventLog:
    """append/read interface for one channel. stateless - construct freely."""

    def __init__(self, user_uuid: str, platform: str):
        self.user_uuid = user_uuid
        self.platform = platform

    # --- writes -------------------------------------------------------------

    def append_message(
        self,
        author_type: str,
        author: str,
        content: str,
        message_type: str = "conversation",
    ) -> Event:
        """record a conversational message (user or agent)."""
        return self._append(
            author_type=author_type, author=author, kind="message",
            content=content, message_type=message_type, metadata={},
        )

    def _append(self, *, author_type, author, kind, content, message_type, metadata) -> Event:
        with get_db() as db:
            row = ConversationEvent(
                user_uuid=self.user_uuid,
                platform=self.platform,
                author_type=author_type,
                author=author,
                kind=kind,
                content=content,
                message_type=message_type,
                event_metadata=metadata,
            )
            db.add(row)
            db.flush()  # get the id + server defaults while the session is open
            event = Event.from_row(row)
            db.commit()
        logger.debug(
            "logged %s event (%s/%s) for user %s", kind, author_type, author, self.user_uuid
        )
        return event

    # --- reads --------------------------------------------------------------

    def recent(self, message_limit: int = 30) -> List[Event]:
        """the last `message_limit` MESSAGE events plus every action event
        interleaved among them, in id order.

        windowing counts only kind='message', so the limit keeps meaning
        "N conversational turns" - action events ride along inside the window
        instead of eating it.
        """
        with get_db() as db:
            message_ids = [
                mid for (mid,) in db.query(ConversationEvent.id).filter(
                    ConversationEvent.user_uuid == self.user_uuid,
                    ConversationEvent.platform == self.platform,
                    ConversationEvent.kind == "message",
                ).order_by(ConversationEvent.id.desc()).limit(message_limit).all()
            ]
            if not message_ids:
                return []
            window_start = min(message_ids)

            rows = db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
                ConversationEvent.platform == self.platform,
                ConversationEvent.id >= window_start,
            ).order_by(ConversationEvent.id).all()
            return [Event.from_row(r) for r in rows]

    def last_message(self) -> Optional[Event]:
        """the most recent MESSAGE event - the scheduler's send-decision input.
        action events are invisible here by construction, so a trailing tool
        action can never masquerade as 'the assistant just replied'."""
        with get_db() as db:
            row = db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
                ConversationEvent.platform == self.platform,
                ConversationEvent.kind == "message",
            ).order_by(ConversationEvent.id.desc()).first()
            return Event.from_row(row) if row else None

    # --- maintenance ----------------------------------------------------------

    def clear(self) -> None:
        """wipe this channel's log (debug/reset affordance)."""
        with get_db() as db:
            db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
                ConversationEvent.platform == self.platform,
            ).delete()
            db.commit()
        logger.info("cleared event log for user %s on %s", self.user_uuid, self.platform)


def cleanup_old_events(max_per_user: int = 1000) -> None:
    """trim each user's log to the most recent N events (run periodically)."""
    with get_db() as db:
        users = db.query(ConversationEvent.user_uuid).distinct().all()
        for (user_uuid,) in users:
            cutoff_row = db.query(ConversationEvent.id).filter(
                ConversationEvent.user_uuid == user_uuid,
            ).order_by(ConversationEvent.id.desc()).offset(max_per_user).first()
            if cutoff_row is None:
                continue
            db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == user_uuid,
                ConversationEvent.id <= cutoff_row[0],
            ).delete()
        db.commit()
    logger.info("cleaned up old conversation events")
