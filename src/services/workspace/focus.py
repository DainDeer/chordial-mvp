"""the focus clock: per-task pomodoro time behind the web focus view.

progress is a bank, not a countdown. each (user, task) pair owns one
TaskFocus row holding banked seconds; at most one row per user is *running*
(running_since non-null). switching tasks banks the outgoing task's live
time and starts the incoming one - nothing is ever lost on a switch, which
is the whole point of the design ("progress saves when switching task").

the clock is server-authoritative: elapsed = accumulated + (now -
running_since) while running. the frontend only ever renders a snapshot and
ticks it forward locally; every mutation round-trips through here, so a
refreshed page always agrees with reality.

not a workspace entity: no lifecycle, no public id, no tool exposure. task
status stays WorkspaceStore's job - the web layer composes the two (start ->
in_progress, complete -> done) rather than this store reaching into tasks.
"""
from __future__ import annotations

from typing import Optional

from src.database.database import get_db
from src.database.models import Task, TaskFocus
from src.services.workspace import vocab
from src.utils.timezone_utils import utc_now


class FocusStore:

    def snapshot(self, user_uuid: str) -> dict:
        """the live focus picture: which task is running (if any) and every
        task's total elapsed seconds, live delta included."""
        now = utc_now()
        with get_db() as db:
            rows = db.query(TaskFocus).filter(
                TaskFocus.user_uuid == user_uuid).all()
            active_task_id: Optional[int] = None
            seconds: dict[int, int] = {}
            for row in rows:
                total = row.accumulated_seconds or 0
                if row.running_since is not None:
                    active_task_id = row.task_id
                    total += max(0, int((now - row.running_since).total_seconds()))
                if total or row.running_since is not None:
                    seconds[row.task_id] = total
            return {"active_task_id": active_task_id, "seconds": seconds}

    def start(self, user_uuid: str, task_id: int) -> dict:
        """start (or resume) the clock on a task, banking whatever was
        running first - the switch and the resume are the same operation.
        only open tasks can hold the clock."""
        now = utc_now()
        with get_db() as db:
            task = db.query(Task).filter(
                Task.user_uuid == user_uuid, Task.id == task_id).first()
            if task is None:
                raise ValueError(f"task {task_id} not found")
            if vocab.is_closed_status("task", task.status):
                raise ValueError(
                    f"task {task_id} is closed ({vocab.display(task.status)}) - "
                    "reopen it before focusing")
            self._bank_running(db, user_uuid, now)
            row = db.query(TaskFocus).filter(
                TaskFocus.user_uuid == user_uuid,
                TaskFocus.task_id == task_id).first()
            if row is None:
                row = TaskFocus(user_uuid=user_uuid, task_id=task_id,
                                accumulated_seconds=0)
                db.add(row)
            row.running_since = now
            db.flush()
        return self.snapshot(user_uuid)

    def pause(self, user_uuid: str) -> dict:
        """bank the running clock, if any. pausing an already-paused board
        is a no-op, not an error - the button is idempotent."""
        with get_db() as db:
            self._bank_running(db, user_uuid, utc_now())
        return self.snapshot(user_uuid)

    def stop(self, user_uuid: str, task_id: int) -> None:
        """bank and stop the clock for one specific task if it's the one
        running - the completion path (done / won't do) calls this so a
        closed task never keeps accruing."""
        with get_db() as db:
            row = db.query(TaskFocus).filter(
                TaskFocus.user_uuid == user_uuid,
                TaskFocus.task_id == task_id,
                TaskFocus.running_since.isnot(None)).first()
            if row is not None:
                self._bank(row, utc_now())

    @staticmethod
    def _bank(row: TaskFocus, now) -> None:
        elapsed = int((now - row.running_since).total_seconds())
        row.accumulated_seconds = (row.accumulated_seconds or 0) + max(0, elapsed)
        row.running_since = None

    def _bank_running(self, db, user_uuid: str, now) -> None:
        """bank every running row (the invariant says there's at most one,
        but a sweep costs the same and self-heals if it's ever violated)."""
        rows = db.query(TaskFocus).filter(
            TaskFocus.user_uuid == user_uuid,
            TaskFocus.running_since.isnot(None)).all()
        for row in rows:
            self._bank(row, now)
