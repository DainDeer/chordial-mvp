"""the sync contract's server side (docs/ROOMS_DESIGN.md section 10).

the sidecar's outbox delivers device-origin events at-least-once; this module
guarantees exactly-once apply. every event carries a client-generated
event_uuid and a per-device monotonic seq; both are unique-constrained, so a
duplicate apply is a no-op that still counts toward the ACK. the ACK is a
durable cursor - the highest CONTIGUOUSLY applied seq for the device - and
the outbox trims at the cursor and retries everything after it.

events are append-only facts. anything that mutates canonical rows (tasks,
cycles) is NOT a sync write - it goes through the server-side stores, which
own the invariants. later phases subscribe processors to applied events
(pip's observations, cycle progress); this module only lands them safely.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from config import Config
from src.database.database import get_db
from src.database.models import Device, DeviceEvent
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# event types the server accepts. a typo'd type is a rejected event, loudly,
# rather than a silently unprocessable row. grows with the phases.
KNOWN_EVENT_TYPES = frozenset({
    "focus_block.started",
    "focus_block.completed",
    "focus_block.abandoned",
    "drift.detected",
    "return.detected",
    "session.started",
    "session.ended",
})


class SyncResult:
    """what one batch apply did, in ACK-cursor terms."""

    def __init__(self, acked_seq: int, applied: int, duplicates: int,
                 rejected: list[dict]):
        self.acked_seq = acked_seq
        self.applied = applied
        self.duplicates = duplicates
        self.rejected = rejected

    def as_dict(self) -> dict:
        return {
            "acked_seq": self.acked_seq,
            "applied": self.applied,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
        }


class QuotaExceeded(RuntimeError):
    """the per-user daily event cap; the endpoint maps this to 429."""


def _validate(raw: object) -> tuple[Optional[dict], Optional[str]]:
    """shape-check one event; returns (clean, None) or (None, error)."""
    if not isinstance(raw, dict):
        return None, "event must be an object"
    event_id = raw.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None, "id (string event uuid) required"
    try:
        uuid_mod.UUID(event_id)
    except ValueError:
        return None, "id must be a uuid"
    seq = raw.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return None, "seq (positive integer) required"
    event_type = raw.get("type")
    if event_type not in KNOWN_EVENT_TYPES:
        return None, f"unknown event type: {event_type!r}"
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        return None, "payload must be an object"
    occurred_at = None
    occurred_raw = raw.get("occurred_at")
    if occurred_raw is not None:
        if not isinstance(occurred_raw, str):
            return None, "occurred_at must be an iso datetime string"
        try:
            parsed = datetime.fromisoformat(occurred_raw)
        except ValueError:
            return None, "occurred_at must be an iso datetime string"
        # stored naive utc like every other timestamp in the schema
        occurred_at = (parsed.replace(tzinfo=None) if parsed.tzinfo is None
                       else (parsed - parsed.utcoffset()).replace(tzinfo=None))
    return {
        "id": event_id.lower(),
        "seq": seq,
        "type": event_type,
        "payload": payload,
        "occurred_at": occurred_at,
    }, None


def _daily_count(db, user_uuid: str, now: datetime) -> int:
    day_start = now - timedelta(days=1)
    return db.query(func.count(DeviceEvent.id)).filter(
        DeviceEvent.user_uuid == user_uuid,
        DeviceEvent.applied_at >= day_start,
    ).scalar() or 0


def apply_events(device_id: int, events: list) -> SyncResult:
    """apply one outbox batch for a device; always returns the durable cursor.

    per-event outcomes: applied (new row), duplicate (event_uuid or
    (device, seq) already landed - no-op that still ACKs), or rejected
    (shape/type error - reported by id, never blocks the rest of the batch).
    the cursor advances over contiguous seqs only; a gap holds it, the
    outbox re-sends from the cursor, and the already-landed later events
    dedupe to no-ops on the retry.
    """
    if len(events) > Config.SYNC_MAX_BATCH:
        raise ValueError(
            f"batch too large (max {Config.SYNC_MAX_BATCH} events)")

    now = utc_now()
    rejected: list[dict] = []
    clean: list[dict] = []
    for raw in events:
        validated, error = _validate(raw)
        if validated is None:
            rid = raw.get("id") if isinstance(raw, dict) else None
            rejected.append({"id": rid, "error": error})
        else:
            clean.append(validated)
    # apply in seq order regardless of batch order - contiguity is the point
    clean.sort(key=lambda e: e["seq"])

    applied = duplicates = 0
    with get_db() as db:
        device = db.query(Device).filter(Device.id == device_id).one()
        if clean and (_daily_count(db, device.user_uuid, now) + len(clean)
                      > Config.SYNC_DAILY_EVENT_CAP):
            raise QuotaExceeded(
                f"daily event cap ({Config.SYNC_DAILY_EVENT_CAP}) exceeded")

        landed_seqs = set()
        for event in clean:
            try:
                with db.begin_nested():
                    db.add(DeviceEvent(
                        event_uuid=event["id"],
                        device_id=device.id,
                        user_uuid=device.user_uuid,
                        seq=event["seq"],
                        event_type=event["type"],
                        payload=event["payload"],
                        occurred_at=event["occurred_at"],
                        applied_at=now,
                    ))
                applied += 1
            except IntegrityError:
                duplicates += 1
            landed_seqs.add(event["seq"])

        # advance the cursor over everything now contiguous - both this
        # batch's rows and any earlier-landed rows past a since-filled gap
        cursor = device.acked_seq
        if landed_seqs:
            existing = {
                s for (s,) in db.query(DeviceEvent.seq).filter(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.seq > cursor,
                ).all()
            }
            while cursor + 1 in existing:
                cursor += 1
            if cursor != device.acked_seq:
                device.acked_seq = cursor
        db.commit()

    if rejected:
        logger.warning("sync: device %s batch had %d rejected events",
                       device_id, len(rejected))
    return SyncResult(cursor, applied, duplicates, rejected)
