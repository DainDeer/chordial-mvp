"""rooms: the bounded-conversation store (docs/ROOMS_DESIGN.md sections 4+12).

the lifecycle lives here, in exactly one place, as the section-4 idempotent
state machine: open -> closing -> closed. daily rooms are LAZY - created by
the first interaction of a user's local day, closed by the arrival of the
next day's first interaction (never a midnight cron; a 1am "i can't sleep"
lands in monday's room where it belongs).

the close pass here is v0-deterministic: a structured digest built from the
room's rows (counts + the last exchange), written under the one-summary-per-
room unique constraint. no turn ever waits on an LLM job - and when an LLM
close pass arrives, it upgrades the CONTENT of the summary, not the shape of
this machine.

the pre-rooms world is grandfathered on first touch: a user with legacy
conversation_events but no rooms gets a room_type='legacy' room whose
room_uuid IS their user uuid - exactly the stream_id their old rows were
backfilled with, so history, resolution, and archives all line up for free.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from src.database.database import get_db
from src.database.models import ConversationEvent, Room, RoomSummary
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

_CLIP = 200


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _CLIP else text[:_CLIP - 1] + "…"


class RoomStore:
    """all room reads and writes; invariants live here, not in callers."""

    # --- the main entry point -------------------------------------------------

    def current_room(self, user_uuid: str, today: date_type) -> dict:
        """today's daily room, lazily created - THE call every inbound
        interaction makes. closes any staler open daily rooms first (the
        lazy close trigger), grandfathering the legacy stream on first
        touch. `today` is the user's LOCAL date (caller resolves timezone).
        returns a detached {id, room_uuid, room_type, status, date}."""
        self._grandfather_legacy(user_uuid)
        self._close_stale_dailies(user_uuid, before=today)

        with get_db() as db:
            row = db.query(Room).filter(
                Room.user_uuid == user_uuid,
                Room.room_type == "daily",
                Room.date == today,
            ).first()
            if row is not None:
                return self._detach(row)
        try:
            with get_db() as db:
                row = Room(user_uuid=user_uuid, room_type="daily",
                           date=today)
                db.add(row)
                db.commit()
                logger.info("daily room created for %s (%s)", user_uuid, today)
                return self._detach(row)
        except IntegrityError:
            # the lazy-create race: someone else won; read their monday
            with get_db() as db:
                row = db.query(Room).filter(
                    Room.user_uuid == user_uuid,
                    Room.room_type == "daily",
                    Room.date == today,
                ).one()
                return self._detach(row)

    # --- lifecycle ------------------------------------------------------------

    def close_room(self, room_id: int) -> None:
        """open/closing -> closed, idempotently: mark closing, write the
        digest under the unique constraint, mark closed. safe to re-run at
        any point of a previous partial close."""
        with get_db() as db:
            room = db.query(Room).filter(Room.id == room_id).first()
            if room is None or room.status == "closed":
                return
            if room.status == "open":
                room.status = "closing"
                db.commit()

        digest = self._digest(room_id)
        with get_db() as db:
            room = db.query(Room).filter(Room.id == room_id).one()
            try:
                with db.begin_nested():
                    db.add(RoomSummary(room_id=room.id,
                                       user_uuid=room.user_uuid,
                                       content=digest))
            except IntegrityError:
                pass  # a previous close already wrote it; keep theirs
            room.status = "closed"
            room.closed_at = utc_now()
            db.commit()
        logger.info("room %s closed", room_id)

    def _close_stale_dailies(self, user_uuid: str, before: date_type) -> None:
        with get_db() as db:
            stale = [r.id for r in db.query(Room).filter(
                Room.user_uuid == user_uuid,
                Room.room_type == "daily",
                Room.status != "closed",
                Room.date < before,
            ).all()]
        for room_id in stale:
            self.close_room(room_id)
        self._refresh_stale_summaries(user_uuid)

    def _refresh_stale_summaries(self, user_uuid: str) -> None:
        """closing races the engine: an activation already generating on
        yesterday's stream may land its reply AFTER the summary froze (the
        store accepts late appends on purpose - the action trail must
        survive). rather than coordinate locks across processes, the sweep
        is eventually-correct: any closed room whose newest event postdates
        its summary gets its digest recomputed on the next interaction."""
        with get_db() as db:
            candidates = db.query(RoomSummary, Room).join(
                Room, Room.id == RoomSummary.room_id,
            ).filter(
                RoomSummary.user_uuid == user_uuid,
                Room.status == "closed",
            ).all()
            to_refresh = []
            for summary, room in candidates:
                newest = db.query(func.max(ConversationEvent.created_at)).filter(
                    ConversationEvent.stream_id == room.room_uuid,
                ).scalar()
                if newest is not None and newest > summary.created_at:
                    to_refresh.append((summary.id, room.id))
        for summary_id, room_id in to_refresh:
            digest = self._digest(room_id)
            with get_db() as db:
                db.query(RoomSummary).filter(
                    RoomSummary.id == summary_id,
                ).update({"content": digest, "created_at": utc_now()},
                         synchronize_session=False)
                db.commit()
            logger.info("room %s summary refreshed after late events", room_id)

    def _grandfather_legacy(self, user_uuid: str) -> None:
        """first touch of the rooms era: if the user has pre-rooms history
        and no legacy room yet, enroll that stream as a closed legacy room
        (room_uuid == user_uuid == the backfilled stream_id)."""
        with get_db() as db:
            existing = db.query(Room).filter(
                Room.user_uuid == user_uuid,
                Room.room_type == "legacy",
            ).first()
            if existing is not None:
                if existing.status != "closed":
                    # a crash mid-grandfather (or the loser of the creation
                    # race) left it unfinished; close_room resumes from any
                    # partial state
                    room_id = existing.id
                else:
                    return
            else:
                room_id = None
        if room_id is not None:
            self.close_room(room_id)
            return
        with get_db() as db:
            has_history = db.query(ConversationEvent.id).filter(
                ConversationEvent.stream_id == user_uuid,
            ).first() is not None
            if not has_history:
                return
            try:
                with db.begin_nested():
                    db.add(Room(room_uuid=user_uuid, user_uuid=user_uuid,
                                room_type="legacy", status="open"))
                db.commit()
            except IntegrityError:
                return  # raced; the winner's room stands
            room_id = db.query(Room.id).filter(
                Room.room_uuid == user_uuid).scalar()
        self.close_room(room_id)
        logger.info("legacy stream grandfathered for %s", user_uuid)

    # --- reads ----------------------------------------------------------------

    def get_by_uuid(self, room_uuid: str) -> Optional[dict]:
        with get_db() as db:
            row = db.query(Room).filter(Room.room_uuid == room_uuid).first()
            return self._detach(row) if row is not None else None

    def user_of_room(self, room_uuid: str) -> Optional[str]:
        """the rooms half of identity.resolve_stream_user."""
        with get_db() as db:
            return db.query(Room.user_uuid).filter(
                Room.room_uuid == room_uuid).scalar()

    def latest_summary(self, user_uuid: str) -> Optional[dict]:
        """the newest closed room's summary - the next room's hydration."""
        with get_db() as db:
            row = db.query(RoomSummary, Room.date, Room.room_type).join(
                Room, Room.id == RoomSummary.room_id,
            ).filter(
                RoomSummary.user_uuid == user_uuid,
            ).order_by(RoomSummary.id.desc()).first()
            if row is None:
                return None
            summary, room_date, room_type = row
            return {"content": summary.content,
                    "date": room_date.isoformat() if room_date else None,
                    "room_type": room_type}

    def recent_rooms(self, user_uuid: str, limit: int = 10) -> list[dict]:
        with get_db() as db:
            rows = db.query(Room).filter(
                Room.user_uuid == user_uuid,
            ).order_by(Room.id.desc()).limit(limit).all()
            return [self._detach(r) for r in rows]

    # --- the v0 close digest --------------------------------------------------

    def _digest(self, room_id: int) -> str:
        """deterministic compressed consequences: counts + the last exchange.
        an LLM close pass upgrades this content later; the shape (one text
        block the next room hydrates with) is the stable part.

        SHARED-CHANNEL ONLY: the summary hydrates every helper's next-room
        prompt, so dm-scope events (a private 1:1 with one helper) are
        excluded entirely - from the counts, the last exchange, everything.
        per-helper summary variants can exist later; leaking never can."""
        def _shared(row) -> bool:
            return (row.event_metadata or {}).get("scope") != "dm"

        with get_db() as db:
            room = db.query(Room).filter(Room.id == room_id).one()
            rows = [r for r in db.query(ConversationEvent).filter(
                ConversationEvent.stream_id == room.room_uuid,
                ConversationEvent.kind == "message",
            ).order_by(ConversationEvent.id).all() if _shared(r)]
            n_actions = sum(1 for r in db.query(ConversationEvent).filter(
                ConversationEvent.stream_id == room.room_uuid,
                ConversationEvent.kind == "action",
            ).all() if _shared(r))

            n_user = sum(1 for r in rows if r.author_type == "user")
            n_agent = sum(1 for r in rows if r.author_type == "agent")
            label = (room.date.isoformat() if room.date
                     else f"{room.room_type} room")
            lines = [f"{label}: {n_user} user messages, {n_agent} helper "
                     f"replies, {n_actions} actions."]
            last_user = next((r for r in reversed(rows)
                              if r.author_type == "user"), None)
            last_agent = next((r for r in reversed(rows)
                               if r.author_type == "agent"), None)
            if last_user is not None or last_agent is not None:
                lines.append("last exchange:")
                if last_user is not None:
                    lines.append(f"  user: {_clip(last_user.content)}")
                if last_agent is not None:
                    lines.append(
                        f"  {last_agent.author}: {_clip(last_agent.content)}")
            return "\n".join(lines)

    @staticmethod
    def _detach(row: Room) -> dict:
        return {
            "id": row.id,
            "room_uuid": row.room_uuid,
            "user_uuid": row.user_uuid,
            "room_type": row.room_type,
            "status": row.status,
            "date": row.date.isoformat() if row.date else None,
        }


_store: Optional[RoomStore] = None


def get_room_store() -> RoomStore:
    global _store
    if _store is None:
        _store = RoomStore()
    return _store
