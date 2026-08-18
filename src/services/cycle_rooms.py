"""the cycle-room doors (phase 6b, docs/ROOMS_DESIGN.md sections 4+8).

two doors open off a sealed cycle: the retrospective and the planning
room. both anchor to the cycle just lived - the retro looks back at it,
and the planning room's evidence base (the retro's summary, the filed
scorecard) belongs to it even though the conversation plans the next one.

the retro door is where edwin finally SPEAKS a scorecard. the honesty
chain, in order:

- opening the retro is a subpoena: if the card isn't filed yet, the
  scorer runs on demand with the grace waived (trigger='retro') - the
  person showing up for the review has called the evidence question, and
  a watermark meant for absent devices must not hold their retro hostage.
  completeness is never waived: no sealed cycle, no retro.
- edwin's presentation is seeded exactly once per card, and REPAIRED on
  every open: an authored frame around a deterministic render of the
  ALREADY-FILED card (scores from arithmetic, prose edwin wrote at
  scoring time). no new model call, no invented numbers - the same
  discipline as the deer's authored lines. a durable marker in the
  event's metadata is the presented/not-presented state, so a retro that
  first opened cardless (scoring failed, no scorer wired) or crashed
  between creation and seeding presents the card on a LATER open - the
  "arithmetic will catch up" note is a promise this module keeps.
- the planning door seeds nothing. its context arrives through hydration
  (the retro summary + the card render); the first voice in a planning
  room is the person's.

this module is a service seam, not a store: RoomStore owns room
invariants, CycleScorer owns the card, and this stitches them behind the
api doors.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from src.database.database import get_db
from src.database.models import ConversationEvent, Cycle
from src.services import cycle_scorer
from src.services.rooms import RoomStore, get_room_store
from src.services.workspace import vocab

logger = logging.getLogger(__name__)

# edwin's authored frames around the rendered card. the render is the
# ledger's; these lines are the character's, fixed here so the voice
# never drifts with a model. no question-ending (council voice rule).
# the LATE frame is for a card presented on a repair pass - the room
# already heard "the arithmetic will catch up", and now it has.
_PRESENTATION_OPEN = "*opens the ledger* the record for '{title}', as filed."
_PRESENTATION_LATE = ("*returns with the ledger* the card for '{title}', "
                      "now written - as promised.")
_PRESENTATION_CLOSE = ("the numbers are arithmetic from the ledger; the "
                       "prose is mine. i'm most interested in what the "
                       "record doesn't show.")

# durable presentation state, carried in the seeded event's metadata: the
# repair pass looks for the presentation marker, so a crash between room
# creation and seeding - or a card that files after a cardless first open -
# still ends with exactly one presented card. the pending note carries its
# own marker and never counts as a presentation.
_PRESENTED = "scorecard_presentation"
_PENDING_NOTE = "scorecard_pending"


class CycleRooms:
    """the door service: reads the latest sealed cycle, ensures the card,
    opens the rooms, seeds the presentation. `scorer` may be None (no
    on-demand scoring - the retro still opens once the sweep has filed)."""

    def __init__(self, scorer=None, room_store: Optional[RoomStore] = None):
        self.scorer = scorer
        self.rooms = room_store or get_room_store()
        # serializes the presented-check + seed pair within this process
        # (the single-writer deployment); the metadata marker is the
        # durable state it checks
        self._present_lock = threading.Lock()

    # --- the read model -------------------------------------------------------

    def doors(self, user_uuid: str) -> Optional[dict]:
        """the door state: the latest sealed cycle plus whichever of its
        rooms exist. None when there is nothing to look back on yet."""
        brief = self._latest_sealed(user_uuid)
        if brief is None:
            return None
        rooms = self.rooms.cycle_rooms_for(user_uuid, brief["public_id"])
        assessment = cycle_scorer.assessment_for(user_uuid,
                                                 brief["public_id"])
        return {
            "cycle": brief,
            "scored": assessment is not None,
            "retro": _public(rooms.get("cycle_retro")),
            "planning": _public(rooms.get("cycle_planning")),
        }

    # --- the doors --------------------------------------------------------------

    async def open_retro(self, user_uuid: str) -> Optional[dict]:
        """get-or-create-or-reopen the retro room for the latest sealed
        cycle, scoring on demand when the card isn't filed yet, and
        repairing the presentation on EVERY open - a room that first
        opened cardless (or crashed before seeding) presents the card the
        moment one exists. returns {room, cycle, created} or None when no
        sealed cycle exists."""
        brief = self._latest_sealed(user_uuid)
        if brief is None:
            return None
        subject_id = brief["public_id"]

        assessment = cycle_scorer.assessment_for(user_uuid, subject_id)
        if assessment is None and self.scorer is not None:
            # the subpoena: the person called the question. a lost race
            # against the sweep is fine - either way the card exists after.
            try:
                await self.scorer.score_cycle(user_uuid, brief["id"],
                                              trigger="retro")
            except Exception:
                logger.exception("on-demand scoring failed for %s; the "
                                 "retro opens without a card", subject_id)
            assessment = cycle_scorer.assessment_for(user_uuid, subject_id)

        room, created = self.rooms.open_cycle_room(
            user_uuid, "cycle_retro", subject_id)
        self._ensure_presented(user_uuid, room["room_uuid"],
                               brief, assessment)
        return {"room": _public(room), "cycle": brief, "created": created}

    async def open_planning(self, user_uuid: str) -> Optional[dict]:
        """get-or-create the planning room following the latest sealed
        cycle. seeds nothing: hydration carries the retro's compressed
        consequences, and the first voice here is the person's."""
        brief = self._latest_sealed(user_uuid)
        if brief is None:
            return None
        room, created = self.rooms.open_cycle_room(
            user_uuid, "cycle_planning", brief["public_id"])
        return {"room": _public(room), "cycle": brief, "created": created}

    # --- internals --------------------------------------------------------------

    @staticmethod
    def _latest_sealed(user_uuid: str) -> Optional[dict]:
        """the most recently closed complete cycle, as a small brief. the
        person's close (closed_at) orders the past; rows missing one
        (hand-edited) sort by id as the honest fallback."""
        with get_db() as db:
            row = db.query(Cycle).filter(
                Cycle.user_uuid == user_uuid,
                Cycle.status == "complete",
            ).order_by(Cycle.closed_at.desc().nullslast(),
                       Cycle.id.desc()).first()
            if row is None:
                return None
            return {
                "id": row.id,
                "public_id": vocab.public_id("cycle", row.id),
                "title": row.title,
                "theme": row.theme,
                "start_date": (row.start_date.isoformat()
                               if row.start_date else None),
                "end_date": (row.end_date.isoformat()
                             if row.end_date else None),
                "closed_at": (row.closed_at.isoformat()
                              if row.closed_at else None),
            }

    def _ensure_presented(self, user_uuid: str, room_uuid: str,
                          brief: dict, assessment: Optional[dict]) -> None:
        """edwin's presentation, made durable and repairable. the seeded
        event carries a metadata marker; every open checks the marker and
        seeds whatever is missing:

        - card filed, never presented -> present it (with the first-visit
          frame on a quiet room, the 'now written - as promised' frame
          when the room already heard the pending note)
        - no card, no note yet -> the honest pending note, once
        - already presented -> nothing (walking back in reseeds nothing)

        the markers are the state, not the created flag - so a crash
        between room creation and seeding, or a card that only files after
        a cardless first open, still converges on exactly one note and
        exactly one presented card."""
        title = brief.get("title") or brief["public_id"]
        with self._present_lock:
            with get_db() as db:
                rows = db.query(ConversationEvent).filter(
                    ConversationEvent.stream_id == room_uuid,
                    ConversationEvent.author == cycle_scorer.SCORER_ID,
                    ConversationEvent.kind == "message",
                ).all()
                notes = {(r.event_metadata or {}).get("note_type")
                         for r in rows}
            if assessment is not None:
                if _PRESENTED in notes:
                    return
                frame = (_PRESENTATION_LATE if _PENDING_NOTE in notes
                         else _PRESENTATION_OPEN)
                content = "\n\n".join([
                    frame.format(title=title),
                    cycle_scorer.render_assessment(assessment),
                    _PRESENTATION_CLOSE,
                ])
                self._speak(user_uuid, room_uuid, content, _PRESENTED)
                logger.info("edwin presented %s in retro room %s",
                            brief["public_id"], room_uuid)
                return
            if _PENDING_NOTE not in notes:
                self._speak(
                    user_uuid, room_uuid,
                    f"*taps the ledger* the card for '{title}' isn't "
                    "written yet, i'm afraid. the record of the "
                    "conversation still counts - we can look back "
                    "properly, and the arithmetic will catch up.",
                    _PENDING_NOTE)

    @staticmethod
    def _speak(user_uuid: str, room_uuid: str, content: str,
               note_type: str) -> None:
        with get_db() as db:
            db.add(ConversationEvent(
                user_uuid=user_uuid,
                stream_id=room_uuid,
                platform="app",
                author_type="agent",
                author=cycle_scorer.SCORER_ID,
                kind="message",
                content=content,
                message_type="conversation",
                event_metadata={"note_type": note_type},
            ))
            db.commit()


def _public(room: Optional[dict]) -> Optional[dict]:
    """a room as the api speaks it (no user_uuid, api field names)."""
    if room is None:
        return None
    return {
        "id": room["room_uuid"],
        "type": room["room_type"],
        "status": room["status"],
        "subject_id": room["subject_id"],
    }
