"""focus, owned locally (docs/ROOMS_DESIGN.md section 5) - the task-clock
model: one task's clock runs at a time, and time BANKS.

a *run* is one stretch of attention on one task. pausing ends the run and
banks its seconds; switching tasks pauses the old run and starts a new one;
nothing here is ever "abandoned" - stopping is information the bank keeps.
the block target (default 25 minutes) is a rhythm, not a wall: when the
clock crosses it the deer says so ONCE and the run keeps counting overtime
until the person lands it themselves. finishing means the TASK is done -
the celebration moment - and emits focus_block.completed for the server
(pip starts reading these in phase 5).

the clock is wall-time arithmetic on stored timestamps, so it works
offline and a sidecar restart resumes mid-run exactly where reality is.
per-task banked totals reset at the machine's local midnight - the sidecar
is on-device; the device's day IS the person's day.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from src.sidecar.store import SidecarStore

DEFAULT_TARGET_MINUTES = 25.0
MAX_TARGET_MINUTES = 120.0
MIN_TARGET_MINUTES = 0.05     # tiny targets stay possible for dev smoke runs
_LABEL_CAP = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class FocusError(ValueError):
    """a request the focus state can't honor."""


class FocusEngine:
    """the one-running-clock state machine. `clock` is injectable so tests
    move time instead of sleeping through it."""

    def __init__(self, store: SidecarStore,
                 clock: Callable[[], datetime] = _utc_now):
        self.store = store
        self.clock = clock
        # run ids whose target-crossing line already fired (in-memory: a
        # restart may re-announce once, which is gentler than never)
        self._announced: set[int] = set()

    # --- reads ---------------------------------------------------------------

    def _day_start_iso(self) -> str:
        """local midnight, as utc - banked bars are a per-day story and the
        device's day is the person's day."""
        local = self.clock().astimezone()
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).isoformat()

    def state(self) -> dict:
        """the deer window's whole world: the running clock (if any) and
        today's banked seconds per task."""
        banked = self.store.banked_since(self._day_start_iso())
        active = self.store.active_run()
        if active is None:
            return {"running": False, "banked": banked}
        now = self.clock()
        run_seconds = max(
            0, int((now - _parse(active["started_at"])).total_seconds()))
        target_seconds = int(active["target_minutes"] * 60)
        return {
            "running": True,
            "task_id": active["task_id"],
            "label": active["label"],
            "target_minutes": active["target_minutes"],
            "started_at": active["started_at"],
            "run_seconds": run_seconds,
            "over_target": run_seconds >= target_seconds,
            "banked": banked,
        }

    # --- transitions -----------------------------------------------------------

    def start(self, task_id: Optional[int] = None,
              label: Optional[str] = None,
              target_minutes: float = DEFAULT_TARGET_MINUTES) -> dict:
        """run the clock on a task. if another task's clock is running this
        IS the switch: the old run pauses (banks) first. starting the task
        that's already running is a no-op question, not an error."""
        if not isinstance(target_minutes, (int, float)) \
                or isinstance(target_minutes, bool) \
                or not (MIN_TARGET_MINUTES <= target_minutes
                        <= MAX_TARGET_MINUTES):
            raise FocusError(
                f"target_minutes must be between {MIN_TARGET_MINUTES} "
                f"and {MAX_TARGET_MINUTES}")
        if task_id is not None and (not isinstance(task_id, int)
                                    or isinstance(task_id, bool)
                                    or task_id < 1):
            raise FocusError("task_id must be a positive integer")
        label = (label or "").strip()[:_LABEL_CAP] or None

        active = self.store.active_run()
        if active is not None:
            if active["task_id"] == task_id and task_id is not None:
                raise FocusError("that task's clock is already running")
            self._end_run(active, "switched")

        now = self.clock()
        self.store.insert_run(task_id, label, float(target_minutes),
                              now.isoformat())
        self.store.enqueue("session.started",
                           {"task_id": task_id, "label": label},
                           occurred_at=now.isoformat())
        return self.state()

    def pause(self) -> Optional[dict]:
        """stop the clock, bank the run. returns the closed run's summary,
        or None when nothing was running."""
        active = self.store.active_run()
        if active is None:
            return None
        return self._end_run(active, "paused")

    def finish(self) -> dict:
        """the task is DONE - the celebration transition. banks the running
        run and emits focus_block.completed with today's whole banked total
        for the task. requires a running clock (finishing is an act of
        landing, not of tidying)."""
        active = self.store.active_run()
        if active is None:
            raise FocusError("no clock is running")
        summary = self._end_run(active, "finished")
        banked = self.store.banked_since(self._day_start_iso())
        self.store.enqueue(
            "focus_block.completed",
            {"task_id": active["task_id"], "label": active["label"],
             "run_seconds": summary["seconds"],
             "banked_seconds_today": banked.get(active["task_id"] or 0, 0)},
            occurred_at=summary["ended_at"])
        return summary

    def crossed_target(self) -> bool:
        """True exactly once per run, the moment its clock crosses the
        target - the ding, without ending anything."""
        active = self.store.active_run()
        if active is None or active["id"] in self._announced:
            return False
        now = self.clock()
        elapsed = (now - _parse(active["started_at"])).total_seconds()
        if elapsed >= active["target_minutes"] * 60:
            self._announced.add(active["id"])
            return True
        return False

    def _end_run(self, active: dict, reason: str) -> dict:
        now = self.clock()
        seconds = max(
            0, int((now - _parse(active["started_at"])).total_seconds()))
        self.store.close_run(active["id"], now.isoformat(), seconds, reason)
        self._announced.discard(active["id"])
        self.store.enqueue(
            "session.ended",
            {"task_id": active["task_id"], "label": active["label"],
             "seconds": seconds, "reason": reason},
            occurred_at=now.isoformat())
        return {"task_id": active["task_id"], "label": active["label"],
                "seconds": seconds, "reason": reason,
                "ended_at": now.isoformat()}
