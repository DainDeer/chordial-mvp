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
- edwin's presentation is seeded exactly once, at room creation: an
  authored frame around a deterministic render of the ALREADY-FILED card
  (scores from arithmetic, prose edwin wrote at scoring time). no new
  model call, no invented numbers - the same discipline as the deer's
  authored lines.
- the planning door seeds nothing. its context arrives through hydration
  (the retro summary + the card render); the first voice in a planning
  room is the person's.

this module is a service seam, not a store: RoomStore owns room
invariants, CycleScorer owns the card, and this stitches them behind the
api doors.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.database.database import get_db
from src.database.models import ConversationEvent, Cycle
from src.services import cycle_scorer
from src.services.rooms import RoomStore, get_room_store
from src.services.workspace import vocab

logger = logging.getLogger(__name__)

# edwin's authored frame around the rendered card. the render is the
# ledger's; these two lines are the character's, fixed here so the voice
# never drifts with a model. no question-ending (council voice rule).
_PRESENTATION_OPEN = "*opens the ledger* the record for '{title}', as filed."
_PRESENTATION_CLOSE = ("the numbers are arithmetic from the ledger; the "
                       "prose is mine. i'm most interested in what the "
                       "record doesn't show.")


class CycleRooms:
    """the door service: reads the latest sealed cycle, ensures the card,
    opens the rooms, seeds the presentation. `scorer` may be None (no
    on-demand scoring - the retro still opens once the sweep has filed)."""

    def __init__(self, scorer=None, room_store: Optional[RoomStore] = None):
        self.scorer = scorer
        self.rooms = room_store or get_room_store()

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
        """get-or-create the retro room for the latest sealed cycle,
        scoring on demand when the card isn't filed yet. returns
        {room, cycle, created} or None when no sealed cycle exists."""
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
        if created:
            self._seed_presentation(user_uuid, room["room_uuid"],
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

    def _seed_presentation(self, user_uuid: str, room_uuid: str,
                           brief: dict, assessment: Optional[dict]) -> None:
        """edwin's one seeded message: the filed card, framed. runs only on
        room creation (the unique subject index makes creation happen once,
        so the presentation can't double). a retro that opened cardless
        (scorer absent or the cycle reopened mid-flight) gets an honest
        note instead of silence."""
        title = brief.get("title") or brief["public_id"]
        if assessment is not None:
            body = cycle_scorer.render_assessment(assessment)
            content = "\n\n".join([
                _PRESENTATION_OPEN.format(title=title),
                body,
                _PRESENTATION_CLOSE,
            ])
        else:
            content = (f"*taps the ledger* the card for '{title}' isn't "
                       "written yet, i'm afraid. the record of the "
                       "conversation still counts - we can look back "
                       "properly, and the arithmetic will catch up.")
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
            ))
            db.commit()
        logger.info("edwin presented %s in retro room %s",
                    brief["public_id"], room_uuid)


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
