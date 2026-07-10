"""the event log: one USER's ordered record of everything that happened -
their messages, agent replies, agent tool actions, system notes.

a user has ONE conversation, spanning platforms: discord and telegram are two
doors into the same room. each event carries a `platform` tag as provenance
(where that moment happened - drives delivery targeting and switch detection),
but reads never filter on it; history is unified.

deliberately db-backed with NO in-memory message cache: once the orchestrator
writes action events alongside messages, a long-lived in-memory list is a
cache-coherence bug factory, and reading ~50 rows from local WAL sqlite per
turn costs nothing. sqlite is the cache.

ordering: single writer + sqlite autoincrement means id order IS insertion
order IS chronology. every read here orders by id, never created_at (which is
kept for humans and elapsed-time math).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
import json
import logging

from src.database.database import get_db
from src.database.models import ConversationEvent
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# caps on the frozen action line, so one chatty tool call can't permanently
# occupy a wall of cache-stable history bytes
_ACTION_INPUT_CAP = 300
_ACTION_RESULT_CAP = 300


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "…"


def _scope_meta(scope: str, with_helper: Optional[str]) -> dict:
    """scope tags for an event's metadata. the common 'group' case writes NO
    tag (absence means group), so group-only history is byte-identical to
    pre-dm history and old warm caches survive."""
    if scope == "group" and with_helper is None:
        return {}
    meta = {"scope": scope}
    if with_helper is not None:
        meta["with_helper"] = with_helper
    return meta


def format_action_line(name: str, tool_input: dict, result_content: str) -> str:
    """the one-line action rendering, frozen into the event's content at write
    time. deterministic serialization (sorted keys) + write-once storage means
    the bytes replayed into prompts can never drift - the renderer emits this
    string verbatim, never re-serializing from metadata."""
    try:
        input_json = json.dumps(tool_input or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        input_json = str(tool_input)
    result = " ".join((result_content or "").split())  # collapse newlines/runs
    return f"{name} {_clip(input_json, _ACTION_INPUT_CAP)} -> {_clip(result, _ACTION_RESULT_CAP)}"


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
    platform: Optional[str] = None   # provenance: where this moment happened

    @property
    def role(self) -> str:
        """the prompt-side view of the author: agents (and system) render on
        the assistant side of the wire, humans on the user side."""
        return "user" if self.author_type == "user" else "assistant"

    @property
    def scope(self) -> str:
        """'group' (the shared channel, visible to every helper) or 'dm' (a
        private 1:1 with one helper). absence of the tag means 'group' - all v2
        history predates dms, so it reads as the shared channel it always was,
        keeping those bytes cache-identical."""
        return self.metadata.get("scope", "group")

    @property
    def dm_helper(self) -> Optional[str]:
        """for a dm-scope event, which helper the 1:1 is with. an agent's own
        dm line is also attributable via `author`; this tags the USER's dm
        lines (whose author is just 'user') to the right private channel."""
        return self.metadata.get("with_helper")

    def visible_to(self, helper_id: Optional[str]) -> bool:
        """can `helper_id` see this event? group events: everyone. dm events:
        only the helper the dm is with (as its partner, or as its own author).
        helper_id=None means 'no privacy filter' (the unified, all-scopes view
        the scheduler and switch-detection want)."""
        if helper_id is None or self.scope != "dm":
            return True
        return self.dm_helper == helper_id or self.author == helper_id

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
            platform=row.platform,
        )


class EventLog:
    """append/read interface for one user's conversation. stateless -
    construct freely."""

    def __init__(self, user_uuid: str):
        self.user_uuid = user_uuid

    # --- writes -------------------------------------------------------------

    def append_message(
        self,
        author_type: str,
        author: str,
        content: str,
        message_type: str = "conversation",
        platform: Optional[str] = None,
        scope: str = "group",
        with_helper: Optional[str] = None,
    ) -> Event:
        """record a conversational message (user or agent). `platform` is
        provenance - where this message happened/was delivered. `scope`='dm'
        (with `with_helper` naming the 1:1 partner) marks a private message
        only that helper should ever see replayed; the default 'group' is the
        shared channel, and writes no scope tag so v2 history stays byte-stable."""
        return self._append(
            author_type=author_type, author=author, kind="message",
            content=content, message_type=message_type,
            metadata=_scope_meta(scope, with_helper),
            platform=platform,
        )

    def append_action(
        self,
        author: str,
        name: str,
        tool_input: dict,
        result_content: str,
        platform: Optional[str] = None,
        scope: str = "group",
        with_helper: Optional[str] = None,
    ) -> Event:
        """record one executed tool call as an action event. the promptable
        one-liner is frozen into `content` here, at write time; metadata keeps
        the raw pieces for debugging. `scope`/`with_helper` mirror
        append_message - a dm action stays inside that private channel."""
        meta = {"tool": name, "input": tool_input, "result": result_content[:1000]}
        meta.update(_scope_meta(scope, with_helper))
        return self._append(
            author_type="agent", author=author, kind="action",
            content=format_action_line(name, tool_input, result_content),
            message_type=None,
            metadata=meta,
            platform=platform,
        )

    def append_note(
        self,
        content: str,
        *,
        platform: Optional[str],
        metadata: Optional[dict] = None,
    ) -> Event:
        """record a system note - an observation about the conversation (e.g.
        the platform-switch notice) that is never rendered into prompts and
        never counts as 'the assistant replied' for the scheduler."""
        return self._append(
            author_type="system", author="system", kind="note",
            content=content, message_type=None, metadata=metadata or {},
            platform=platform,
        )

    def _append(self, *, author_type, author, kind, content, message_type,
                metadata, platform=None) -> Event:
        with get_db() as db:
            row = ConversationEvent(
                user_uuid=self.user_uuid,
                platform=platform,
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

    def recent(self, message_limit: int = 30, visible_to: Optional[str] = None) -> List[Event]:
        """the last `message_limit` MESSAGE events plus every action event
        interleaved among them, in id order - across all platforms (one
        conversation, however many doors into it).

        windowing counts only kind='message', so the limit keeps meaning
        "N conversational turns" - action events ride along inside the window
        instead of eating it.

        `visible_to` (a helper id) applies the dm privacy filter: that helper
        sees the group channel plus its OWN dms, never a sibling's. left None
        (the scheduler, switch-detection, reconciler) it's the unified view.
        the None path is byte-for-byte the pre-dm query, so group-only history
        stays cache-identical.
        """
        if visible_to is None:
            return self._recent_unfiltered(message_limit)

        # privacy path: pull a generous tail, drop what this helper can't see,
        # THEN window on the visible messages. bounded pull keeps it cheap at
        # mvp scale (a user's whole log is trimmed to ~1000 events anyway).
        # detach into Event dataclasses INSIDE the session - the orm rows are
        # invalid once it closes.
        with get_db() as db:
            rows = db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
            ).order_by(ConversationEvent.id.desc()).limit(message_limit * 6 + 200).all()
            events = [Event.from_row(r) for r in reversed(rows)]
        events = [e for e in events if e.visible_to(visible_to)]
        return self._window(events, message_limit)

    def _recent_unfiltered(self, message_limit: int) -> List[Event]:
        with get_db() as db:
            message_ids = [
                mid for (mid,) in db.query(ConversationEvent.id).filter(
                    ConversationEvent.user_uuid == self.user_uuid,
                    ConversationEvent.kind == "message",
                ).order_by(ConversationEvent.id.desc()).limit(message_limit).all()
            ]
            if not message_ids:
                return []
            window_start = min(message_ids)

            rows = db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
                ConversationEvent.id >= window_start,
            ).order_by(ConversationEvent.id).all()
            return [Event.from_row(r) for r in rows]

    @staticmethod
    def _window(events: List[Event], message_limit: int) -> List[Event]:
        """trim an id-ordered event list to the last `message_limit` MESSAGE
        events plus the actions interleaved among them (from the first kept
        message onward) - the same 'N turns, actions ride along' shape the
        unfiltered query produces, applied to an already-filtered list."""
        msg_positions = [i for i, e in enumerate(events) if e.kind == "message"]
        if not msg_positions:
            return []
        start = msg_positions[-message_limit] if len(msg_positions) > message_limit else msg_positions[0]
        return events[start:]

    def last_message(self) -> Optional[Event]:
        """the most recent MESSAGE event - the scheduler's send-decision input.
        action/note events are invisible here by construction, so a trailing
        tool action or switch notice can never masquerade as 'the assistant
        just replied'."""
        with get_db() as db:
            row = db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
                ConversationEvent.kind == "message",
            ).order_by(ConversationEvent.id.desc()).first()
            return Event.from_row(row) if row else None

    def last_user_message(self) -> Optional[Event]:
        """the most recent message from the HUMAN - its platform is the
        'active platform' (where they last chose to talk)."""
        with get_db() as db:
            row = db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
                ConversationEvent.kind == "message",
                ConversationEvent.author_type == "user",
            ).order_by(ConversationEvent.id.desc()).first()
            return Event.from_row(row) if row else None

    def active_platform(self) -> Optional[str]:
        """where the user last spoke - the delivery target for proactive
        sends. None when they've never messaged (delivery fallback is
        UserManager.resolve_delivery_identity's job, not ours)."""
        event = self.last_user_message()
        return event.platform if event else None

    # --- maintenance ----------------------------------------------------------

    def clear(self) -> None:
        """wipe this user's whole log (debug/reset affordance)."""
        with get_db() as db:
            db.query(ConversationEvent).filter(
                ConversationEvent.user_uuid == self.user_uuid,
            ).delete()
            db.commit()
        logger.info("cleared event log for user %s", self.user_uuid)


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
