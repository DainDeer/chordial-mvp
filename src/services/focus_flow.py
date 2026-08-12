"""applied device events become consequences: pip's noticing, exactly once.

the sync module (device_sync.py) only LANDS events safely; this processor
walks rows whose `processed_at` is still null and turns them into their
downstream consequences, stamping `processed_at` in the SAME transaction -
so apply-then-process is crash-safe: a crash between the two simply leaves
an unstamped row for the next pass, and a pass is always safe to re-run.

v1 consequences (ROOMS_DESIGN sections 5 + 12, the vertical slice):
- `focus_block.completed` becomes ONE of pip's progress observations -
  the squirrel keeps the ledger of landed blocks; evidence carries the
  event uuid and the honest numbers.
- every other known type is just stamped: per-run banks (session.ended)
  and drift facts feed the cycle projection and future reviews straight
  from the event rows themselves - cycle progress needs no processor.

called after every sync apply (cheap: the unprocessed set is indexed and
almost always tiny) and safe to call from anywhere else.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.database.database import get_db
from src.database.models import DeviceEvent, Observation
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# the squirrel has focus and cycles - completed blocks are her lane
PIP_ID = "pip"

_BATCH_LIMIT = 200


def process_pending(user_uuid: Optional[str] = None,
                    limit: int = _BATCH_LIMIT) -> int:
    """process up to `limit` unprocessed applied events (optionally one
    user's), committing consequences and processed_at stamps atomically.
    returns how many rows were stamped."""
    now = utc_now()
    with get_db() as db:
        q = db.query(DeviceEvent).filter(
            DeviceEvent.processed_at.is_(None),
            DeviceEvent.rejected.is_(False),
        )
        if user_uuid is not None:
            q = q.filter(DeviceEvent.user_uuid == user_uuid)
        rows = q.order_by(DeviceEvent.id).limit(limit).all()
        for row in rows:
            try:
                if row.event_type == "focus_block.completed":
                    db.add(_pip_noticing(row))
            except Exception:
                # a malformed payload must not wedge the pipeline: log it,
                # stamp it, move on - the event row itself keeps the truth
                logger.exception("focus_flow: event %s consequence failed",
                                 row.event_uuid)
            row.processed_at = now
        db.commit()
        return len(rows)


def _pip_noticing(row: DeviceEvent) -> Observation:
    """one progress observation per landed block: structured noticing for
    reviews and edwin's scorecards, never replayed as conversation."""
    payload = row.payload or {}
    label = payload.get("label")
    run_seconds = _seconds(payload.get("run_seconds"))
    banked = _seconds(payload.get("banked_seconds_today"))
    what = f'"{label}"' if isinstance(label, str) and label.strip() else \
        "a focus block"
    content = (f"landed {what} - {_minutes(run_seconds)} min this run, "
               f"{_minutes(banked)} min banked today")
    return Observation(
        user_uuid=row.user_uuid,
        helper_id=PIP_ID,
        kind="progress",
        content=content,
        evidence={
            "event_uuid": row.event_uuid,
            "task_id": payload.get("task_id"),
            "run_seconds": run_seconds,
            "banked_seconds_today": banked,
            "occurred_at": (row.occurred_at.isoformat()
                            if row.occurred_at else None),
        },
    )


def _seconds(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _minutes(seconds: int) -> int:
    return max(1, round(seconds / 60)) if seconds else 0
