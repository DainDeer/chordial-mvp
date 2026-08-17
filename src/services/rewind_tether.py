"""the tether: rewind's second door (docs/REWIND_DESIGN.md section 8).

the desktop card only fires on *return* - if the person never comes back to
the desk, the block runs unattended and no question ever shows. this module
covers exactly that case: a `rewind.offered` event that stays unresolved for
the away threshold earns ONE phone ping with two inline choices, and the
answer rides back down to the sidecar as a pending decision.

three parts, deliberately separated:

- `fold_event` (called from focus_flow's exactly-once processor) turns the
  rewind.* / return.detected event stream into one tracking row per offer.
  the row is a shadow, never the truth - the sidecar owns the offer.
- `RewindTether.sweep_once` (a supervised loop in main) pings rows that are
  due: opted in, unresolved, no return seen since the offer, quiet hours
  respected, and not stale. `pinged_at` is claimed atomically BEFORE the
  send, so racing sweeps can never double-ping; a transiently failed send
  un-claims for one more try, a permanent one keeps the claim (the link is
  being deactivated anyway).
- `record_decision_token` (called by the bots' button handlers) lands the
  answer. first answer wins via a conditional UPDATE; a tap after the desk
  already resolved gets told so, gently.

the sidecar is authoritative end to end: it polls `pending_decisions` via
GET /api/v1/sync/decisions, applies or refuses each one, and the offer's
eventual terminal event (rewind.applied / rewind.kept) is what closes the
row here - never the decision itself. amounts in the ping are estimates
("about 25m"); the real excision arithmetic happens on-device at apply time.

opt-in is separate from having a linked phone (sol's design review): the
ping fires only for users whose schedule_preferences carry
`"rewind_tether": true`, set through conversation (preference_tools).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import or_

from config import Config
from src.database.database import get_db
from src.database.models import PlatformIdentity, RewindDecision, User
from src.personas import CHAIR_ID
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# the event types fold_event knows how to fold. return.detected rides along
# because "has the person been back since the question was asked?" is the
# tether's whole notion of 'away' - any desk interaction (a card tap, a
# pause) implies a return landed first, so this one check also keeps pings
# away from held blocks whose clocks are already stopped.
FOLDED_TYPES = frozenset({
    "rewind.offered",
    "rewind.applied",
    "rewind.kept",
    "rewind.undone",
    "return.detected",
})

# platforms the tether may ring. the app is deliberately absent - the deer's
# card IS the desktop surface; a second desktop ping would be a second voice.
TETHER_PLATFORMS = ("telegram", "discord")

CHOICES = ("remove", "keep")


# --- folding (runs inside focus_flow's exactly-once claim) -----------------

def fold_event(db, row) -> None:
    """fold one applied device event into its rewind_decisions row. runs in
    focus_flow's transaction, after the row was claimed - so each event
    folds exactly once, in seq order per device."""
    at = row.occurred_at or row.applied_at
    payload = row.payload or {}

    if row.event_type == "return.detected":
        # the person is back at THIS device's desk: its open questions are
        # now the card's business, not the phone's. presence is scoped to
        # the device (sol's #72 review): offer_uuids are device-local, and
        # a return on the laptop says nothing about the desktop whose card
        # is still waiting in an empty room.
        db.query(RewindDecision).filter(
            RewindDecision.user_uuid == row.user_uuid,
            RewindDecision.device_id == row.device_id,
            RewindDecision.closed_at.is_(None),
        ).update({RewindDecision.returned_at: at},
                 synchronize_session=False)
        return

    offer_uuid = payload.get("offer_uuid")
    if not isinstance(offer_uuid, str) or not offer_uuid:
        logger.warning("tether: %s event %s has no offer_uuid",
                       row.event_type, row.event_uuid)
        return

    tracked = db.query(RewindDecision).filter(
        RewindDecision.offer_uuid == offer_uuid).first()

    if row.event_type == "rewind.offered":
        contested = payload.get("contested_seconds")
        contested = int(contested) if isinstance(contested, (int, float)) \
            and not isinstance(contested, bool) else 0
        label = payload.get("label")
        label = label if isinstance(label, str) else None
        if tracked is None:
            db.add(RewindDecision(
                user_uuid=row.user_uuid, offer_uuid=offer_uuid,
                device_id=row.device_id,
                label=label, contested_seconds=max(0, contested),
                offered_at=at))
        elif tracked.closed_at is None:
            # a re-emit: the question's span grew (a return happened and a
            # NEW quiet joined it), so the away clock re-arms from here
            tracked.contested_seconds = max(0, contested)
            tracked.label = label or tracked.label
            tracked.offered_at = at
        # a closed row ignores a late offered - out-of-order delivery must
        # never resurrect a settled question
        return

    if row.event_type in ("rewind.applied", "rewind.kept"):
        if tracked is None:
            # terminal before offered (reordered across batches): land the
            # row already closed so the offered can never arm a ping
            db.add(RewindDecision(
                user_uuid=row.user_uuid, offer_uuid=offer_uuid,
                device_id=row.device_id, offered_at=at, closed_at=at))
        else:
            tracked.closed_at = at
        return

    if row.event_type == "rewind.undone":
        if tracked is None:
            return
        # the question is open again. undo happens at the desk, so the row
        # re-arms fresh: a NEW away threshold, a NEW single ping if the
        # person walks off without answering. the old answer is history.
        tracked.closed_at = None
        tracked.choice = None
        tracked.decided_at = None
        tracked.source = None
        tracked.pinged_at = None
        tracked.ping_platform = None
        tracked.offered_at = at
        tracked.returned_at = at    # they're at the desk right now
        return


# --- the sidecar's poll -----------------------------------------------------

def pending_decisions(user_uuid: str) -> list[dict]:
    """decided-but-unclosed rows for one user - what GET
    /api/v1/sync/decisions returns. idempotent by design: the sidecar
    refuses what it already resolved, and the terminal event closes the
    row; until then the same decision rides every poll harmlessly."""
    with get_db() as db:
        rows = db.query(RewindDecision).filter(
            RewindDecision.user_uuid == user_uuid,
            RewindDecision.choice.isnot(None),
            RewindDecision.closed_at.is_(None),
        ).order_by(RewindDecision.decided_at).all()
        return [{
            "offer_uuid": r.offer_uuid,
            "choice": r.choice,
            "decided_at": (r.decided_at.isoformat()
                           if r.decided_at else None),
            "source": r.source,
        } for r in rows]


# --- the answer (called from bot button handlers) ---------------------------

def record_decision_token(platform: str, platform_user_id: str,
                          token: str) -> str:
    """land one button tap. `token` is the callback data we minted
    ('rw:<offer_uuid>:<choice>'); the tap is tenant-checked against the
    tapping account's linked user, and first answer wins via a conditional
    UPDATE. returns the ack line to show the person."""
    parts = token.split(":")
    if len(parts) != 3 or parts[0] != "rw" or parts[2] not in CHOICES:
        return "that button has gone stale - the card on the desk still works."
    offer_uuid, choice = parts[1], parts[2]

    with get_db() as db:
        identity = db.query(PlatformIdentity).filter(
            PlatformIdentity.platform == platform,
            PlatformIdentity.platform_user_id == platform_user_id,
        ).first()
        if identity is None or not identity.user_uuid:
            return "i don't recognize this account - link it first."
        tracked = db.query(RewindDecision).filter(
            RewindDecision.offer_uuid == offer_uuid).first()
        if tracked is None or tracked.user_uuid != identity.user_uuid:
            return "i can't find that question anymore."
        settled = _settled_ack(tracked)
        if settled is not None:
            return settled
        claimed = db.query(RewindDecision).filter(
            RewindDecision.id == tracked.id,
            RewindDecision.choice.is_(None),
            RewindDecision.closed_at.is_(None),
        ).update({RewindDecision.choice: choice,
                  RewindDecision.decided_at: utc_now(),
                  RewindDecision.source: platform},
                 synchronize_session=False)
        db.commit()
        if not claimed:
            # lost the race between the read and the claim (sol's #72
            # review): describe the answer that actually WON, not the tap
            # that lost - re-read and let the row speak for itself
            db.expire_all()
            fresh = db.query(RewindDecision).filter(
                RewindDecision.id == tracked.id).first()
            settled = _settled_ack(fresh) if fresh is not None else None
            return settled or "that question just settled another way."
        if choice == "remove":
            return ("got it - the quiet comes off the clock and the "
                    "block rests. the desk makes it official.")
        return ("got it - keeping every minute, and the block pauses. "
                "the desk makes it official.")


def _settled_ack(row: RewindDecision) -> Optional[str]:
    """the ack for a row that already has its outcome - composed from the
    row's actual state, never from the tap that arrived late. None while
    the question is still open."""
    if row.closed_at is not None:
        return "this one's already settled - the desk answered first."
    if row.choice is not None:
        kept = "keeping every minute" if row.choice == "keep" \
            else "the quiet comes off"
        return f"holding to your first answer - {kept}."
    return None


# --- the watcher ------------------------------------------------------------

class RewindTether:
    """the sweep loop: one supervised task beside the pulse. needs the
    router (its outbound reach) and the user manager (delivery identity);
    everything else is table state."""

    def __init__(self, router, user_manager, clock=utc_now):
        self.router = router
        self.user_manager = user_manager
        self.clock = clock

    async def run(self) -> None:
        while True:
            await asyncio.sleep(Config.REWIND_TETHER_SWEEP_SECONDS)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rewind tether sweep hiccup; continuing")

    async def sweep_once(self) -> int:
        """one pass: claim due rows, send each ping. returns pings sent."""
        due = await asyncio.to_thread(self._claim_due)
        sent = 0
        for item in due:
            if await self._ping(item):
                sent += 1
        return sent

    def _claim_due(self) -> list[dict]:
        """find and atomically claim rows that have earned their ping:
        open, undecided, un-pinged, past the away threshold, not stale,
        no return since the (latest) offered, user opted in, and not in
        their quiet hours. the claim (pinged_at) happens here, BEFORE the
        send - losing a ping to a crash is acceptable, sending two is not."""
        now = self.clock()
        threshold = now - timedelta(
            minutes=Config.REWIND_TETHER_AWAY_MINUTES)
        stale_floor = now - timedelta(hours=Config.REWIND_TETHER_STALE_HOURS)
        claimed: list[dict] = []
        with get_db() as db:
            rows = db.query(RewindDecision).filter(
                RewindDecision.closed_at.is_(None),
                RewindDecision.choice.is_(None),
                RewindDecision.pinged_at.is_(None),
                RewindDecision.offered_at <= threshold,
                RewindDecision.offered_at >= stale_floor,
            ).all()
            for row in rows:
                if row.returned_at is not None \
                        and row.returned_at >= row.offered_at:
                    continue        # they came back; the card has it
                user = db.query(User).filter(
                    User.uuid == row.user_uuid).first()
                if user is None or not user.is_active:
                    continue
                prefs = user.schedule_preferences or {}
                if prefs.get("rewind_tether") is not True:
                    continue        # the tether is separately opt-in
                if _in_quiet_hours(user.timezone, now):
                    continue        # unclaimed; retried until stale
                if self._claim_row(db, row, now):
                    claimed.append({
                        "id": row.id,
                        "user_uuid": row.user_uuid,
                        "offer_uuid": row.offer_uuid,
                        "label": row.label,
                        "contested_seconds": row.contested_seconds,
                        "offered_at": row.offered_at,
                    })
            db.commit()
        return claimed

    @staticmethod
    def _claim_row(db, row, now) -> bool:
        """the atomic one-ping claim. every MUTABLE eligibility predicate
        is repeated inside the conditional update (sol's #72 review) - a
        desk resolution, a phone answer, a return, or a re-arm landing
        between the eligibility read and the claim must make the claim
        miss, never yield a stale ping."""
        return bool(db.query(RewindDecision).filter(
            RewindDecision.id == row.id,
            RewindDecision.pinged_at.is_(None),
            RewindDecision.choice.is_(None),
            RewindDecision.closed_at.is_(None),
            # a newer rewind.offered re-armed the away clock
            RewindDecision.offered_at == row.offered_at,
            # a return since the offered hands the question to the card
            or_(RewindDecision.returned_at.is_(None),
                RewindDecision.returned_at < RewindDecision.offered_at),
        ).update({RewindDecision.pinged_at: now},
                 synchronize_session=False))

    async def _ping(self, item: dict) -> bool:
        """one ping for one claimed row. impact language, estimated: the
        quiet has run uninterrupted since the offer (we checked), so
        contested-at-mint plus elapsed is an honest 'about'. the device
        derives the real number at apply time."""
        target = await self.user_manager.resolve_delivery_identity(
            item["user_uuid"], None,
            [p for p in self.router.platforms() if p in TETHER_PLATFORMS])
        if target is None:
            # no ringable link: keep the claim (retrying every sweep would
            # just re-discover the same silence) and say so once
            logger.info("tether: no deliverable platform for user %s",
                        item["user_uuid"])
            return False
        platform, platform_user_id = target
        now = self.clock()
        elapsed = max(0.0, (now - item["offered_at"]).total_seconds())
        minutes = max(1, round((item["contested_seconds"] + elapsed) / 60))
        label = item["label"]
        what = f'"{label}"' if isinstance(label, str) and label.strip() \
            else "your block"
        text = (f"the clock is still running on {what}, but it's been "
                f"quiet about {minutes}m. either way i'll pause the block "
                "- the question is what the clock keeps.")
        choices = [
            {"data": f"rw:{item['offer_uuid']}:remove",
             "label": f"remove ~{minutes}m & pause"},
            {"data": f"rw:{item['offer_uuid']}:keep",
             "label": "keep all time & pause"},
        ]
        ok = await self.router.deliver_choice_as(
            platform, platform_user_id, text, choices, speaker=CHAIR_ID)
        await asyncio.to_thread(self._settle_claim, item["id"],
                                platform if ok else None, ok)
        return ok

    def _settle_claim(self, row_id: int, platform: Optional[str],
                      delivered: bool) -> None:
        with get_db() as db:
            if delivered:
                db.query(RewindDecision).filter(
                    RewindDecision.id == row_id,
                ).update({RewindDecision.ping_platform: platform},
                         synchronize_session=False)
            else:
                # transient failure or dead link: un-claim for another
                # sweep. a permanently dead link was just deactivated by
                # the router, so the retry resolves a different platform
                # or finds none and goes quiet.
                db.query(RewindDecision).filter(
                    RewindDecision.id == row_id,
                    RewindDecision.choice.is_(None),
                    RewindDecision.closed_at.is_(None),
                ).update({RewindDecision.pinged_at: None},
                         synchronize_session=False)
            db.commit()


def _in_quiet_hours(tz_name: Optional[str], now: datetime) -> bool:
    """same shape as the pulse's QuietHoursGate, locally: a ping about a
    quiet block must not be the thing that wakes anyone up. `now` is naive
    utc; an unresolvable timezone fails closed (no ping)."""
    start, end = Config.QUIET_HOURS_START, Config.QUIET_HOURS_END
    if start == end:
        return False                 # no quiet window configured
    try:
        zone = ZoneInfo(tz_name or "UTC")
    except Exception:
        return True
    local_hour = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone).hour
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end
