"""the deterministic scorecard: phase 6's arithmetic half
(docs/ROOMS_DESIGN.md section 8).

edwin scores the SYSTEM, never the person - and the scores themselves are
never a model's opinion. every number here is computed from the cycle
projection (baseline + scope changes + progress, both sides append-only)
and the session ledger; the utility model's only later contribution is
prose - a summary and findings - each pinned to evidence this module and
the validator can check. that split is the cadenza scorer lesson carried
over: the judgment layer proposes, the ledger disposes.

five components, per the design doc, each `{score, evidence}`:

- execution:      did the planned blocks land?
- prioritization: did the landed blocks go where the FROZEN priorities
                  pointed? (mutable end-state priorities would let a late
                  edit recast every earlier block)
- estimation:     how close were the frozen estimates to reality?
- consistency:    was the work spread across the cycle or crammed?
- sustainability: was the pace one that survives contact with a life?

a component that cannot be honestly computed is `score: None` with the
reason in its evidence - "no data" is a finding, not a failure. there is
deliberately NO overall grade: a single number is exactly the productivity
scoring the covenant forbids.

sessions arrive as INTERVALS on the user's local clock, not endpoints: a
25-minute block that ends at 06:00 ran 05:35-06:00 and is judged over that
whole span - split at midnight for day counting, split at the late-window
edges for sustainability, and clipped to the cycle window so a run that
straddles the boundary contributes exactly its inside part.

pure functions only: no db, no config, no clock. callers assemble the
inputs (the projection dict from CycleStore.projection, per-session local
spans) so tests can drive every shape synthetically.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

COMPONENTS = ("execution", "prioritization", "estimation",
              "consistency", "sustainability")

# priority weights for the prioritization mean: landing blocks on a high
# commitment counts fully; medium and low count less. commitments with no
# FROZEN priority (unprioritized at the freeze, or added afterward) sit
# out of the component entirely (see below).
_PRIORITY_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}

# sustainability's late-night window, user-local [23:00, 06:00)
_LATE_START = 23
_LATE_END = 6


@dataclass(frozen=True)
class SessionSpan:
    """one session.ended event as a user-local interval [start, end),
    pre-converted by the caller (the scorer service owns timezone
    plumbing): start = ended_at - seconds, end = ended_at."""
    start: datetime
    end: datetime


def _component(score: Optional[float], evidence: list[str]) -> dict:
    return {"score": (round(score, 2) if score is not None else None),
            "evidence": evidence}


def _blocks(seconds: int, block_seconds: int) -> float:
    return round(seconds / block_seconds, 1) if block_seconds > 0 else 0.0


def compute_scorecard(projection: dict, sessions: list[SessionSpan],
                      *, today: date, block_seconds: int) -> dict:
    """the five components plus the raw numbers they were computed from.
    `projection` is CycleStore.projection() output (commitments enriched
    with `baseline_priority` when frozen); `sessions` are local session
    spans; `today` is the user-local date the cycle SEALED on (its close),
    bounding consistency's elapsed-days denominator so a cycle closed
    early - or scored after downtime - is judged on the days it lived."""
    commitments = projection.get("commitments") or []
    totals = projection.get("totals") or {}
    cycle = projection.get("cycle") or {}
    frozen = bool(projection.get("frozen"))

    lo, hi = _window(cycle)
    day_seconds: dict[date, int] = {}
    late = total = count = 0
    for span in sessions:
        clipped = _clip(span, lo, hi)
        if clipped is None:
            continue
        start, end = clipped
        secs = int((end - start).total_seconds())
        if secs <= 0:
            continue
        count += 1
        total += secs
        for d, s in _seconds_by_day(start, end).items():
            day_seconds[d] = day_seconds.get(d, 0) + s
        late += _late_seconds(start, end)

    return {
        "components": {
            "execution": _execution(totals),
            "prioritization": _prioritization(commitments, frozen,
                                              block_seconds),
            "estimation": _estimation(commitments, frozen),
            "consistency": _consistency(cycle, day_seconds, today),
            "sustainability": _sustainability(total, late),
        },
        "numbers": _numbers(totals, cycle, today,
                            count=count, total=total, late=late,
                            day_seconds=day_seconds),
    }


# --- the components ----------------------------------------------------------

def _execution(totals: dict) -> dict:
    """planned blocks that actually landed. overshoot clamps at 1.0 - doing
    more than planned is not a defect here (estimation is where the gap
    between plan and reality gets judged)."""
    planned = totals.get("planned_blocks") or 0
    done = totals.get("done_blocks") or 0.0
    if planned <= 0:
        return _component(None, ["nothing was planned, so execution has "
                                 "nothing to measure against"])
    evidence = [f"{done} of {planned} planned blocks landed"]
    if done > planned:
        evidence.append("more landed than was planned - the overshoot "
                        "shows up in estimation, not here")
    return _component(min(1.0, done / planned), evidence)


def _prioritization(commitments: list[dict], frozen: bool,
                    block_seconds: int) -> dict:
    """weighted mean of where attributed time went, judged against the
    priorities AS FROZEN in the baseline - a priority edited near cycle
    end must not recast every earlier block. only commitments carrying a
    frozen priority participate: unprioritized-at-freeze and post-freeze
    additions are neither credit nor blame, and unattributed time sits out
    too (nobody knows where it went, so it cannot be judged)."""
    if not frozen:
        return _component(None, ["the cycle was never frozen, so priorities "
                                 "have no fixed reference to judge against"])
    weighted = 0.0
    prioritized_seconds = 0
    high_secs = total_secs = 0
    for c in commitments:
        secs = c.get("seconds_done") or 0
        if secs <= 0:
            continue
        total_secs += secs
        weight = _PRIORITY_WEIGHT.get(c.get("baseline_priority"))
        if weight is None:
            continue
        prioritized_seconds += secs
        weighted += secs * weight
        if c.get("baseline_priority") == "high":
            high_secs += secs
    if prioritized_seconds <= 0:
        return _component(None, ["no attributed time landed on commitments "
                                 "with a frozen priority; nothing to weigh"])
    evidence = [
        f"{_blocks(high_secs, block_seconds)} of "
        f"{_blocks(prioritized_seconds, block_seconds)} prioritized blocks "
        f"went to high-priority commitments"]
    if total_secs > prioritized_seconds:
        evidence.append(
            f"{_blocks(total_secs - prioritized_seconds, block_seconds)} "
            f"blocks on commitments with no frozen priority sat out of "
            f"this score")
    return _component(weighted / prioritized_seconds, evidence)


def _estimation(commitments: list[dict], frozen: bool) -> dict:
    """per-commitment accuracy against the FROZEN baseline - the projection
    vs baseline diff is the whole point of freezing. accuracy per
    commitment = 1 - |done - planned| / planned, floored at 0; the score
    is the mean. released commitments stay in: releasing moved the scope
    honestly, but the original estimate was still an estimate."""
    if not frozen:
        return _component(None, ["the cycle was never frozen, so there is "
                                 "no baseline to judge estimates against"])
    accuracies = []
    misses: list[tuple[float, str]] = []
    for c in commitments:
        baseline = c.get("baseline_blocks")
        if not isinstance(baseline, (int, float)) or baseline <= 0:
            continue
        done = c.get("blocks_done") or 0.0
        miss = abs(done - baseline) / baseline
        accuracies.append(max(0.0, 1.0 - miss))
        if miss > 0.25:
            tag = " (released)" if c.get("status") == "released" else ""
            misses.append((miss, f"'{c.get('title')}'{tag} planned "
                                 f"{baseline}, landed {done}"))
    if not accuracies:
        return _component(None, ["the baseline holds no block estimates "
                                 "to check"])
    misses.sort(reverse=True)
    evidence = ([m for _, m in misses[:3]]
                or [f"all {len(accuracies)} estimates landed within 25%"])
    return _component(sum(accuracies) / len(accuracies), evidence)


def _consistency(cycle: dict, day_seconds: dict, today: date) -> dict:
    """active days over elapsed cycle days. the denominator stops at
    `today` (the seal date), so a cycle closed early is judged on the days
    it lived, never on days that happened after the close."""
    start = _parse_date(cycle.get("start_date"))
    end = _parse_date(cycle.get("end_date"))
    if start is None:
        return _component(None, ["the cycle has no start date; days cannot "
                                 "be counted"])
    last = min(end or today, today)
    elapsed = (last - start).days + 1
    if elapsed <= 0:
        return _component(None, ["the cycle window has no elapsed days yet"])
    active_days = sorted(d for d, s in day_seconds.items()
                         if s > 0 and start <= d <= last)
    evidence = [f"worked {len(active_days)} of {elapsed} cycle days"]
    gap = _longest_gap(active_days, start, last)
    if gap > 1:
        evidence.append(f"longest quiet stretch: {gap} days")
    return _component(len(active_days) / elapsed, evidence)


def _sustainability(total: int, late: int) -> dict:
    """share of focus time OUTSIDE the late-night window [23:00, 06:00),
    measured over each session's whole span - a block ending at 06:00 ran
    through the late hours and is judged there. v1 measures the one signal
    the ledger already carries honestly; burst detection and rest-day
    arithmetic can join once there is history."""
    if total <= 0:
        return _component(None, ["no focus time in the window; pace cannot "
                                 "be judged"])
    share = late / total
    if late == 0:
        evidence = ["no focus time landed in the late-night hours "
                    "(23:00-06:00)"]
    else:
        evidence = [f"{round(share * 100)}% of focus time landed in the "
                    f"late-night hours (23:00-06:00)"]
    return _component(1.0 - share, evidence)


# --- shared arithmetic -------------------------------------------------------

def _numbers(totals: dict, cycle: dict, today: date, *, count: int,
             total: int, late: int, day_seconds: dict) -> dict:
    """the raw counts the components were derived from - kept beside the
    scores so an assessment is auditable without re-running anything."""
    start = _parse_date(cycle.get("start_date"))
    end = _parse_date(cycle.get("end_date"))
    last = min(end or today, today)
    return {
        "planned_blocks": totals.get("planned_blocks"),
        "done_blocks": totals.get("done_blocks"),
        "capacity_blocks": totals.get("capacity_blocks"),
        "unattributed_seconds": totals.get("unattributed_seconds"),
        "session_count": count,
        "session_seconds": total,
        "active_days": sum(1 for s in day_seconds.values() if s > 0),
        "elapsed_days": ((last - start).days + 1
                         if start is not None and last >= start else None),
        "late_seconds": late,
    }


def _window(cycle: dict):
    """the cycle's user-local datetime window [lo, hi); open on whichever
    side the cycle leaves unset."""
    start = _parse_date(cycle.get("start_date"))
    end = _parse_date(cycle.get("end_date"))
    lo = datetime.combine(start, time.min) if start is not None else None
    hi = (datetime.combine(end + timedelta(days=1), time.min)
          if end is not None else None)
    return lo, hi


def _clip(span: SessionSpan, lo: Optional[datetime],
          hi: Optional[datetime]):
    """the span's inside-the-window part, or None when nothing remains."""
    start = span.start if lo is None else max(span.start, lo)
    end = span.end if hi is None else min(span.end, hi)
    return (start, end) if end > start else None


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> int:
    lo, hi = max(a0, b0), min(a1, b1)
    return int((hi - lo).total_seconds()) if hi > lo else 0


def _seconds_by_day(start: datetime, end: datetime) -> dict[date, int]:
    """split one local interval at midnight: a cross-midnight session is
    real work on both calendar days."""
    out: dict[date, int] = {}
    d = start.date()
    while d <= end.date():
        day0 = datetime.combine(d, time.min)
        secs = _overlap(start, end, day0, day0 + timedelta(days=1))
        if secs > 0:
            out[d] = secs
        d += timedelta(days=1)
    return out


def _late_seconds(start: datetime, end: datetime) -> int:
    """seconds of the interval inside [23:00, 06:00), split per day."""
    late = 0
    d = start.date()
    while d <= end.date():
        day0 = datetime.combine(d, time.min)
        late += _overlap(start, end, day0,
                         day0 + timedelta(hours=_LATE_END))
        late += _overlap(start, end, day0 + timedelta(hours=_LATE_START),
                         day0 + timedelta(days=1))
        d += timedelta(days=1)
    return late


def _longest_gap(active_days: list[date], start: date, last: date) -> int:
    """longest run of workless days inside [start, last], edges included."""
    if not active_days:
        return (last - start).days + 1
    gap = max((active_days[0] - start).days, (last - active_days[-1]).days)
    for earlier, later in zip(active_days, active_days[1:]):
        gap = max(gap, (later - earlier).days - 1)
    return gap


def _parse_date(value) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
