"""the cycle spine: commitments, the frozen baseline, scope changes, and the
projection (docs/ROOMS_DESIGN.md section 6).

the shape of honesty here is append-only bookkeeping around mutable rows:

- COMMITMENTS are first-class rows with stable uuids. focus attribution and
  baseline snapshots reference the uuid, never a title, so progress survives
  renames. the live rows may evolve freely BEFORE the freeze.
- FREEZING writes one immutable snapshot per cycle (`cycle_baselines`,
  unique-constrained). after it, planned capacity may only move through
  `change_scope`, which mutates the live row and appends the ledger row in
  the same transaction. completing a commitment is progress, not scope -
  it stays an ordinary update; RELEASING one removes planned blocks, so
  post-freeze it must arrive as a scope change.
- THE PROJECTION is baseline + scope changes + progress. progress is read
  straight from applied device events (`session.ended` banks every run
  exactly once - pauses and switches included, not just celebrated
  finishes), attributed task-first, then by an unambiguous plan match.
  edwin's planning-accuracy signal is the projection-vs-baseline diff, and
  it stays reliable because both sides are append-only.

stateless like the other stores - construct freely, short sessions, detached
dicts out.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional

from sqlalchemy import func

from config import Config
from src.database.database import get_db
from src.database.models import (Commitment, Cycle, CycleBaseline,
                                 DeviceEvent, Plan, ScopeChange, Task, User)
from src.services.workspace import vocab
from src.utils.timezone_utils import to_user_timezone

_TITLE_CAP = 300
_REASON_CAP = 500
_NEXT_ACTION_CAP = 300


class CycleStoreError(ValueError):
    """invariant violations, phrased for the tool layer to surface as-is."""


class CycleStore:

    # --- commitments ---------------------------------------------------------

    def create_commitment(self, user_uuid: str, cycle_id: int, title: str, *,
                          priority: Optional[str] = None,
                          blocks_planned: Optional[int] = None,
                          next_action: Optional[str] = None,
                          plan_id: Optional[int] = None,
                          task_id: Optional[int] = None,
                          reason: Optional[str] = None) -> dict:
        """add a commitment to a cycle. on a FROZEN cycle this is itself a
        scope movement, so a reason is required and a scope-change row is
        appended alongside."""
        title = (title or "").strip()[:_TITLE_CAP]
        if not title:
            raise CycleStoreError("a commitment needs a title")
        if priority is not None:
            priority = vocab.canonical_value(
                priority, vocab.TASK_PRIORITY, "priority")
        blocks_planned = _coerce_blocks(blocks_planned)
        with get_db() as db:
            cycle = self._owned_cycle(db, user_uuid, cycle_id)
            self._check_links(db, user_uuid, plan_id, task_id)
            frozen = self._baseline_row(db, cycle.id) is not None
            reason = (reason or "").strip()[:_REASON_CAP]
            if frozen and not reason:
                raise CycleStoreError(
                    "this cycle's baseline is frozen - adding a commitment "
                    "moves planned capacity, so it needs a reason (it will "
                    "be recorded as a scope change)")
            row = Commitment(
                user_uuid=user_uuid, cycle_id=cycle.id, title=title,
                status="active", priority=priority,
                blocks_planned=blocks_planned,
                next_action=_clean(next_action, _NEXT_ACTION_CAP),
                plan_id=plan_id, task_id=task_id)
            db.add(row)
            db.flush()
            if frozen:
                db.add(ScopeChange(
                    user_uuid=user_uuid, cycle_id=cycle.id,
                    commitment_uuid=row.uuid, reason=reason,
                    deltas={"action": "add", "title": title,
                            "blocks_planned": {"from": None,
                                               "to": blocks_planned}}))
            db.commit()
            return self._commitment_dict(row)

    def update_commitment(self, user_uuid: str, commitment_id: int,
                          **changes) -> dict:
        """ordinary evolution: title, priority, next_action, links, and any
        change that does NOT move planned capacity on a frozen cycle.
        completing is always ordinary (progress, not scope); resizing or
        releasing after the freeze must go through change_scope."""
        allowed = {"title", "status", "priority", "blocks_planned",
                   "next_action", "plan_id", "task_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise CycleStoreError(
                f"unknown commitment field(s): {', '.join(sorted(unknown))}")
        with get_db() as db:
            row = self._owned_commitment(db, user_uuid, commitment_id)
            frozen = self._baseline_row(db, row.cycle_id) is not None
            if "status" in changes:
                changes["status"] = vocab.canonical_status(
                    "commitment", changes["status"])
            if frozen:
                if "blocks_planned" in changes:
                    raise CycleStoreError(
                        "this cycle's baseline is frozen - resizing a "
                        "commitment is a scope change; use change_scope "
                        "with a reason")
                if changes.get("status") == "released":
                    raise CycleStoreError(
                        "this cycle's baseline is frozen - releasing a "
                        "commitment is a scope change; use change_scope "
                        "with a reason")
            self._apply_changes(db, user_uuid, row, changes)
            db.commit()
            return self._commitment_dict(row)

    def list_commitments(self, user_uuid: str, *,
                         cycle_id: Optional[int] = None,
                         include_closed: bool = True) -> list[dict]:
        with get_db() as db:
            q = db.query(Commitment).filter(
                Commitment.user_uuid == user_uuid)
            if cycle_id is not None:
                q = q.filter(Commitment.cycle_id == cycle_id)
            if not include_closed:
                q = q.filter(Commitment.status.in_(
                    vocab.COMMITMENT_STATUS_OPEN))
            return [self._commitment_dict(r)
                    for r in q.order_by(Commitment.id).all()]

    # --- the freeze ----------------------------------------------------------

    def freeze_baseline(self, user_uuid: str, cycle_id: int) -> dict:
        """write the cycle's immutable baseline snapshot: the accepted
        commitments and allocations, exactly once. everything after this is
        either progress or an explicit scope change."""
        with get_db() as db:
            cycle = self._owned_cycle(db, user_uuid, cycle_id)
            if self._baseline_row(db, cycle.id) is not None:
                raise CycleStoreError(
                    "this cycle is already frozen - the baseline is "
                    "immutable (changes of plan are scope changes)")
            commitments = db.query(Commitment).filter(
                Commitment.cycle_id == cycle.id,
                Commitment.user_uuid == user_uuid,
                Commitment.status.in_(vocab.COMMITMENT_STATUS_OPEN),
            ).order_by(Commitment.id).all()
            if not commitments:
                raise CycleStoreError(
                    "nothing to freeze - the cycle has no active commitments")
            snapshot = {
                "cycle": {
                    "title": cycle.title, "theme": cycle.theme,
                    "capacity_blocks": cycle.capacity_blocks,
                    "start_date": _iso(cycle.start_date),
                    "end_date": _iso(cycle.end_date),
                },
                "commitments": [{
                    "uuid": c.uuid, "title": c.title,
                    "priority": c.priority,
                    "blocks_planned": c.blocks_planned,
                    "next_action": c.next_action,
                    "plan_id": c.plan_id, "task_id": c.task_id,
                } for c in commitments],
            }
            row = CycleBaseline(user_uuid=user_uuid, cycle_id=cycle.id,
                                snapshot=snapshot)
            db.add(row)
            db.commit()
            return {"cycle_id": cycle.id, "frozen_at": _iso(row.created_at),
                    "commitments": len(commitments)}

    def baseline(self, user_uuid: str, cycle_id: int) -> Optional[dict]:
        with get_db() as db:
            row = db.query(CycleBaseline).filter(
                CycleBaseline.cycle_id == cycle_id,
                CycleBaseline.user_uuid == user_uuid).first()
            if row is None:
                return None
            return {"cycle_id": row.cycle_id, "snapshot": row.snapshot,
                    "frozen_at": _iso(row.created_at)}

    # --- scope changes -------------------------------------------------------

    def change_scope(self, user_uuid: str, *, reason: str,
                     commitment_id: Optional[int] = None,
                     cycle_id: Optional[int] = None,
                     blocks_planned: Optional[int] = None,
                     release: bool = False,
                     capacity_blocks: Optional[int] = None) -> dict:
        """the sanctioned move: mutate planned capacity AND append the ledger
        row, atomically. three shapes -
        - resize a commitment (commitment_id + blocks_planned)
        - release a commitment (commitment_id + release=True)
        - move the cycle's own capacity (cycle_id + capacity_blocks)
        works pre-freeze too (the ledger never hurts); post-freeze it is the
        only door."""
        reason = (reason or "").strip()[:_REASON_CAP]
        if not reason:
            raise CycleStoreError(
                "a scope change needs a reason - the user's words, kept")
        if release and blocks_planned is not None:
            raise CycleStoreError(
                "one change at a time: resize or release, not both")
        with get_db() as db:
            if commitment_id is not None:
                row = self._owned_commitment(db, user_uuid, commitment_id)
                if release:
                    if row.status != "active":
                        raise CycleStoreError(
                            "only an active commitment can be released")
                    deltas = {"action": "release",
                              "status": {"from": row.status,
                                         "to": "released"},
                              "blocks_planned": {"from": row.blocks_planned,
                                                 "to": 0}}
                    row.status = "released"
                    row.closed_at = datetime.utcnow()
                elif blocks_planned is not None:
                    new_blocks = _coerce_blocks(blocks_planned)
                    deltas = {"action": "resize",
                              "blocks_planned": {"from": row.blocks_planned,
                                                 "to": new_blocks}}
                    row.blocks_planned = new_blocks
                else:
                    raise CycleStoreError(
                        "a commitment scope change is a resize "
                        "(blocks_planned) or a release")
                change = ScopeChange(
                    user_uuid=user_uuid, cycle_id=row.cycle_id,
                    commitment_uuid=row.uuid, reason=reason, deltas=deltas)
            elif cycle_id is not None and capacity_blocks is not None:
                cycle = self._owned_cycle(db, user_uuid, cycle_id)
                new_cap = _coerce_blocks(capacity_blocks)
                change = ScopeChange(
                    user_uuid=user_uuid, cycle_id=cycle.id,
                    commitment_uuid=None, reason=reason,
                    deltas={"action": "recapacity",
                            "capacity_blocks": {"from": cycle.capacity_blocks,
                                                "to": new_cap}})
                cycle.capacity_blocks = new_cap
            else:
                raise CycleStoreError(
                    "a scope change targets a commitment (resize/release) "
                    "or the cycle's capacity")
            db.add(change)
            db.commit()
            return {"id": change.id, "cycle_id": change.cycle_id,
                    "commitment_uuid": change.commitment_uuid,
                    "reason": change.reason, "deltas": change.deltas,
                    "created_at": _iso(change.created_at)}

    def list_scope_changes(self, user_uuid: str, cycle_id: int) -> list[dict]:
        with get_db() as db:
            rows = db.query(ScopeChange).filter(
                ScopeChange.user_uuid == user_uuid,
                ScopeChange.cycle_id == cycle_id,
            ).order_by(ScopeChange.id).all()
            return [{"id": r.id, "commitment_uuid": r.commitment_uuid,
                     "reason": r.reason, "deltas": r.deltas,
                     "created_at": _iso(r.created_at)} for r in rows]

    # --- the projection ------------------------------------------------------

    def projection(self, user_uuid: str,
                   cycle_id: Optional[int] = None) -> Optional[dict]:
        """the current view: baseline + scope changes + progress. defaults to
        the active cycle; None when there isn't one. progress comes from
        applied `session.ended` device events (every run banks exactly once)
        attributed task-first, then by unambiguous plan match; a run that
        matches nothing lands in `unattributed_seconds` rather than being
        silently dropped or double-counted."""
        block_seconds = Config.POM_MINUTES * 60
        with get_db() as db:
            if cycle_id is not None:
                cycle = self._owned_cycle(db, user_uuid, cycle_id)
            else:
                cycle = db.query(Cycle).filter(
                    Cycle.user_uuid == user_uuid,
                    Cycle.status == "active",
                ).order_by(Cycle.id.desc()).first()
                if cycle is None:
                    return None

            commitments = db.query(Commitment).filter(
                Commitment.cycle_id == cycle.id,
                Commitment.user_uuid == user_uuid,
            ).order_by(Commitment.id).all()
            baseline_row = self._baseline_row(db, cycle.id)
            baseline_blocks = {}
            if baseline_row is not None:
                baseline_blocks = {
                    c["uuid"]: c.get("blocks_planned")
                    for c in baseline_row.snapshot.get("commitments", [])}

            seconds_by_uuid, unattributed = self._progress(
                db, user_uuid, cycle, commitments)

            plan_titles, task_titles = self._link_titles(
                db, user_uuid, commitments)
            commitment_dicts = []
            for c in commitments:
                d = self._commitment_dict(
                    c, plan_title=plan_titles.get(c.plan_id),
                    task_title=task_titles.get(c.task_id))
                secs = seconds_by_uuid.get(c.uuid, 0)
                d["seconds_done"] = secs
                d["blocks_done"] = round(secs / block_seconds, 1)
                d["baseline_blocks"] = baseline_blocks.get(c.uuid)
                commitment_dicts.append(d)

            planned = sum(c.blocks_planned or 0 for c in commitments
                          if c.status != "released")
            done_blocks = round(
                sum(seconds_by_uuid.values()) / block_seconds, 1)
            capacity = cycle.capacity_blocks
            return {
                "cycle": {
                    "id": cycle.id,
                    "public_id": vocab.public_id("cycle", cycle.id),
                    "title": cycle.title, "status": cycle.status,
                    "theme": cycle.theme, "capacity_blocks": capacity,
                    "start_date": _iso(cycle.start_date),
                    "end_date": _iso(cycle.end_date),
                },
                "frozen": baseline_row is not None,
                "frozen_at": (_iso(baseline_row.created_at)
                              if baseline_row is not None else None),
                "commitments": commitment_dicts,
                "scope_changes": [
                    {"id": r.id, "commitment_uuid": r.commitment_uuid,
                     "reason": r.reason, "deltas": r.deltas,
                     "created_at": _iso(r.created_at)}
                    for r in db.query(ScopeChange).filter(
                        ScopeChange.user_uuid == user_uuid,
                        ScopeChange.cycle_id == cycle.id,
                    ).order_by(ScopeChange.id).all()],
                "totals": {
                    "capacity_blocks": capacity,
                    "planned_blocks": planned,
                    "done_blocks": done_blocks,
                    "unattributed_seconds": unattributed,
                    "unallocated_blocks": (capacity - planned
                                           if capacity is not None else None),
                },
            }

    # --- progress attribution ------------------------------------------------

    def _progress(self, db, user_uuid: str, cycle: Cycle,
                  commitments: list[Commitment]) -> tuple[dict, int]:
        """sum banked seconds per commitment uuid from session.ended events
        inside the cycle's date window (user-local, same convention as the
        agenda). returns ({uuid: seconds}, unattributed_seconds)."""
        q = db.query(DeviceEvent).filter(
            DeviceEvent.user_uuid == user_uuid,
            DeviceEvent.rejected.is_(False),
            DeviceEvent.event_type == "session.ended",
        )
        # coarse sql bounds (utc, one day of slack for timezone skew) keep
        # the scan proportional to the cycle; the exact user-local window
        # check happens per-row below
        when_col = func.coalesce(DeviceEvent.occurred_at,
                                 DeviceEvent.applied_at)
        if cycle.start_date is not None:
            q = q.filter(when_col >= datetime.combine(
                cycle.start_date - timedelta(days=1), time.min))
        if cycle.end_date is not None:
            q = q.filter(when_col < datetime.combine(
                cycle.end_date + timedelta(days=2), time.min))
        events = q.all()
        user = db.query(User).filter(User.uuid == user_uuid).first()
        tz_name = (user.timezone if user and user.timezone else "UTC")

        seconds_by_task: dict[Optional[int], int] = {}
        for ev in events:
            payload = ev.payload or {}
            secs = payload.get("seconds")
            if not isinstance(secs, (int, float)) or isinstance(secs, bool) \
                    or secs <= 0:
                continue
            when = ev.occurred_at or ev.applied_at
            if when is not None and not self._in_window(when, cycle, tz_name):
                continue
            task_id = payload.get("task_id")
            if not isinstance(task_id, int) or isinstance(task_id, bool):
                task_id = None
            seconds_by_task[task_id] = (seconds_by_task.get(task_id, 0)
                                        + int(secs))

        by_task = {c.task_id: c for c in commitments
                   if c.task_id is not None}
        by_plan: dict[int, list[Commitment]] = {}
        for c in commitments:
            if c.plan_id is not None:
                by_plan.setdefault(c.plan_id, []).append(c)

        task_ids = [t for t in seconds_by_task if t is not None
                    and t not in by_task]
        task_plan = {}
        if task_ids:
            task_plan = {t.id: t.plan_id for t in db.query(Task).filter(
                Task.user_uuid == user_uuid, Task.id.in_(task_ids)).all()}

        seconds_by_uuid: dict[str, int] = {}
        unattributed = 0
        for task_id, secs in seconds_by_task.items():
            target = by_task.get(task_id) if task_id is not None else None
            if target is None and task_id is not None:
                # plan match only when it is unambiguous - two commitments
                # sharing a plan must not both claim the same minutes
                candidates = by_plan.get(task_plan.get(task_id), [])
                target = candidates[0] if len(candidates) == 1 else None
            if target is not None:
                seconds_by_uuid[target.uuid] = (
                    seconds_by_uuid.get(target.uuid, 0) + secs)
            else:
                unattributed += secs
        return seconds_by_uuid, unattributed

    @staticmethod
    def _in_window(when: datetime, cycle: Cycle, tz_name: str) -> bool:
        """does this (naive utc) moment fall on the cycle's user-local
        calendar? open-ended on whichever side the cycle leaves unset."""
        local_date = to_user_timezone(when, tz_name).date()
        if cycle.start_date is not None and local_date < cycle.start_date:
            return False
        if cycle.end_date is not None and local_date > cycle.end_date:
            return False
        return True

    # --- plumbing ------------------------------------------------------------

    @staticmethod
    def _owned_cycle(db, user_uuid: str, cycle_id: int) -> Cycle:
        row = db.query(Cycle).filter(
            Cycle.id == cycle_id, Cycle.user_uuid == user_uuid).first()
        if row is None:
            raise CycleStoreError(f"no cycle with id {cycle_id}")
        return row

    @staticmethod
    def _owned_commitment(db, user_uuid: str, commitment_id: int) -> Commitment:
        row = db.query(Commitment).filter(
            Commitment.id == commitment_id,
            Commitment.user_uuid == user_uuid).first()
        if row is None:
            raise CycleStoreError(f"no commitment with id {commitment_id}")
        return row

    @staticmethod
    def _baseline_row(db, cycle_id: int) -> Optional[CycleBaseline]:
        return db.query(CycleBaseline).filter(
            CycleBaseline.cycle_id == cycle_id).first()

    @staticmethod
    def _check_links(db, user_uuid: str, plan_id, task_id) -> None:
        if plan_id is not None:
            if db.query(Plan.id).filter(Plan.id == plan_id,
                                        Plan.user_uuid == user_uuid
                                        ).first() is None:
                raise CycleStoreError(f"no plan with id {plan_id}")
        if task_id is not None:
            if db.query(Task.id).filter(Task.id == task_id,
                                        Task.user_uuid == user_uuid
                                        ).first() is None:
                raise CycleStoreError(f"no task with id {task_id}")

    def _apply_changes(self, db, user_uuid: str, row: Commitment,
                       changes: dict) -> None:
        if "title" in changes:
            title = (changes.pop("title") or "").strip()[:_TITLE_CAP]
            if not title:
                raise CycleStoreError("a commitment needs a title")
            row.title = title
        if "priority" in changes:
            value = changes.pop("priority")
            row.priority = (vocab.canonical_value(
                value, vocab.TASK_PRIORITY, "priority")
                if value is not None else None)
        if "next_action" in changes:
            row.next_action = _clean(changes.pop("next_action"),
                                     _NEXT_ACTION_CAP)
        if "blocks_planned" in changes:
            row.blocks_planned = _coerce_blocks(changes.pop("blocks_planned"))
        if "plan_id" in changes or "task_id" in changes:
            plan_id = changes.pop("plan_id", row.plan_id)
            task_id = changes.pop("task_id", row.task_id)
            self._check_links(db, user_uuid, plan_id, task_id)
            row.plan_id, row.task_id = plan_id, task_id
        if "status" in changes:
            status = changes.pop("status")   # already canonical
            row.status = status
            if vocab.is_closed_status("commitment", status):
                if row.closed_at is None:
                    row.closed_at = datetime.utcnow()
            else:
                row.closed_at = None

    @staticmethod
    def _commitment_dict(row: Commitment, plan_title: Optional[str] = None,
                         task_title: Optional[str] = None) -> dict:
        return {
            "id": row.id,
            "public_id": vocab.public_id("commitment", row.id),
            "uuid": row.uuid, "cycle_id": row.cycle_id,
            "title": row.title, "status": row.status,
            "priority": row.priority,
            "blocks_planned": row.blocks_planned,
            "next_action": row.next_action,
            "plan_id": row.plan_id, "plan_title": plan_title,
            "task_id": row.task_id, "task_title": task_title,
            "created_at": _iso(row.created_at),
            "closed_at": _iso(row.closed_at),
        }

    @staticmethod
    def _link_titles(db, user_uuid: str,
                     commitments: list[Commitment]) -> tuple[dict, dict]:
        plan_ids = {c.plan_id for c in commitments if c.plan_id is not None}
        task_ids = {c.task_id for c in commitments if c.task_id is not None}
        plans = {p.id: p.title for p in db.query(Plan).filter(
            Plan.user_uuid == user_uuid, Plan.id.in_(plan_ids)).all()
        } if plan_ids else {}
        tasks = {t.id: t.title for t in db.query(Task).filter(
            Task.user_uuid == user_uuid, Task.id.in_(task_ids)).all()
        } if task_ids else {}
        return plans, tasks


def _coerce_blocks(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value < 0 or value > 1000:
        raise CycleStoreError("blocks must be a number between 0 and 1000")
    return int(value)


def _clean(value: Optional[str], cap: int) -> Optional[str]:
    value = (value or "").strip()[:cap]
    return value or None


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None
