"""focus blocks, owned locally (docs/ROOMS_DESIGN.md section 5).

the pomodoro clock runs HERE, against the local store - it works offline
and never lags on a network call. a block is minutes of effort, not a
promise of completion: the engine records started/completed/abandoned as
derived events in the outbox, and the sync pump carries them to the server
whenever there's a connection.

one block at a time; the clock is wall-time arithmetic on stored
timestamps (started_at/ends_at), so a sidecar restart mid-block resumes
exactly where reality is - and tick() settles a block that expired while
the process was down.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.sidecar.store import SidecarStore

DEFAULT_MINUTES = 25.0
MAX_MINUTES = 120.0
MIN_MINUTES = 0.05          # tiny blocks stay possible for dev smoke runs
_LABEL_CAP = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class SessionError(ValueError):
    """a request the session state can't honor (already running, bad minutes)."""


class SessionEngine:
    """the one-active-block state machine. `clock` is injectable so tests
    can move time instead of sleeping through it."""

    def __init__(self, store: SidecarStore,
                 clock: Callable[[], datetime] = _utc_now):
        self.store = store
        self.clock = clock

    # --- reads ---------------------------------------------------------------

    def state(self) -> dict:
        """the deer window's whole world: the active block or quiet."""
        active = self.store.active_session()
        if active is None:
            return {"active": False}
        now = self.clock()
        ends_at = _parse(active["ends_at"])
        return {
            "active": True,
            "label": active["label"],
            "minutes": active["minutes"],
            "started_at": active["started_at"],
            "ends_at": active["ends_at"],
            "remaining_seconds": max(0, int((ends_at - now).total_seconds())),
        }

    # --- transitions -----------------------------------------------------------

    def start(self, label: Optional[str] = None,
              minutes: float = DEFAULT_MINUTES) -> dict:
        """begin a block. records focus_block.started in the outbox."""
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) \
                or not (MIN_MINUTES <= minutes <= MAX_MINUTES):
            raise SessionError(
                f"minutes must be between {MIN_MINUTES} and {MAX_MINUTES}")
        if self.store.active_session() is not None:
            raise SessionError("a block is already running")
        label = (label or "").strip()[:_LABEL_CAP] or None
        now = self.clock()
        ends = now + timedelta(minutes=minutes)
        self.store.insert_session(label, float(minutes),
                                  now.isoformat(), ends.isoformat())
        self.store.enqueue("focus_block.started",
                           {"label": label, "minutes": minutes},
                           occurred_at=now.isoformat())
        return self.state()

    def stop(self) -> Optional[str]:
        """end the active block now. past the clock it counts as completed
        (the ding just hadn't been acknowledged); before it, abandoned -
        which is information, never failure. returns the outcome or None
        when nothing was running."""
        active = self.store.active_session()
        if active is None:
            return None
        now = self.clock()
        outcome = ("completed" if now >= _parse(active["ends_at"])
                   else "abandoned")
        self._close(active, outcome, now)
        return outcome

    def tick(self) -> Optional[str]:
        """settle an expired block; the server loop calls this every second.
        returns 'completed' the moment a block's clock runs out."""
        active = self.store.active_session()
        if active is None or self.clock() < _parse(active["ends_at"]):
            return None
        self._close(active, "completed", _parse(active["ends_at"]))
        return "completed"

    def _close(self, active: dict, outcome: str, at: datetime) -> None:
        self.store.close_session(active["id"], outcome, at.isoformat())
        elapsed = int((at - _parse(active["started_at"])).total_seconds())
        self.store.enqueue(
            f"focus_block.{outcome}",
            {"label": active["label"], "minutes": active["minutes"],
             "elapsed_seconds": max(0, elapsed)},
            occurred_at=at.isoformat())
