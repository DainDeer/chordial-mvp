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
- prioritization: did the landed blocks go where the priorities pointed?
- estimation:     how close were the frozen estimates to reality?
- consistency:    was the work spread across the cycle or crammed?
- sustainability: was the pace one that survives contact with a life?

a component that cannot be honestly computed is `score: None` with the
reason in its evidence - "no data" is a finding, not a failure. there is
deliberately NO overall grade: a single number is exactly the productivity
scoring the covenant forbids.

pure functions only: no db, no config, no clock. callers assemble the
inputs (the projection dict from CycleStore.projection, per-session rows
already converted to the user's local calendar) so tests can drive every
shape synthetically.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

COMPONENTS = ("execution", "prioritization", "estimation",
              "consistency", "sustainability")

# priority weights for the prioritization mean: landing blocks on a high
# commitment counts fully; medium and low count less. unprioritized
# commitments sit out of the component entirely (see below).
_PRIORITY_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}

# sustainability's late-night window, user-local hours [23:00, 06:00)
_LATE_START = 23
_LATE_END = 6


@dataclass(frozen=True)
class SessionSlice:
    """one session.ended event, pre-converted to the user's local calendar
    by the caller (the scorer service owns timezone plumbing)."""
    seconds: int
    local_date: date
    local_hour: int          # hour the session ENDED, user-local 0..23


def _component(score: Optional[float], evidence: list[str]) -> dict:
    return {"score": (round(score, 2) if score is not None else None),
            "evidence": evidence}


def _blocks(seconds: int, block_seconds: int) -> float:
    return round(seconds / block_seconds, 1) if block_seconds > 0 else 0.0


def compute_scorecard(projection: dict, sessions: list[SessionSlice],
                      *, today: date, block_seconds: int) -> dict:
    """the five components plus the raw numbers they were computed from.
    `projection` is CycleStore.projection() output; `sessions` are the
    cycle-window session.ended slices; `today` is the user-local date the
    scoring runs on (bounds consistency's elapsed-days denominator for a
    cycle scored before its end date, e.g. one closed early)."""
    commitments = projection.get("commitments") or []
    totals = projection.get("totals") or {}
    cycle = projection.get("cycle") or {}
    frozen = bool(projection.get("frozen"))

    return {
        "components": {
            "execution": _execution(totals),
            "prioritization": _prioritization(commitments, block_seconds),
            "estimation": _estimation(commitments, frozen),
            "consistency": _consistency(cycle, sessions, today),
            "sustainability": _sustainability(sessions),
        },
        "numbers": _numbers(totals, sessions, cycle, today),
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


def _prioritization(commitments: list[dict], block_seconds: int) -> dict:
    """weighted mean of where attributed time went: a block on a high
    commitment scores 1.0, medium 0.7, low 0.4. only commitments that
    carry a priority participate - unprioritized time is neither credit
    nor blame. unattributed time sits out too (nobody knows where it
    went, so it cannot be judged)."""
    weighted = 0.0
    prioritized_seconds = 0
    high_secs = total_secs = 0
    for c in commitments:
        secs = c.get("seconds_done") or 0
        if secs <= 0:
            continue
        total_secs += secs
        weight = _PRIORITY_WEIGHT.get(c.get("priority"))
        if weight is None:
            continue
        prioritized_seconds += secs
        weighted += secs * weight
        if c.get("priority") == "high":
            high_secs += secs
    if prioritized_seconds <= 0:
        return _component(None, ["no attributed time landed on prioritized "
                                 "commitments; nothing to weigh"])
    evidence = [
        f"{_blocks(high_secs, block_seconds)} of "
        f"{_blocks(prioritized_seconds, block_seconds)} prioritized blocks "
        f"went to high-priority commitments"]
    if total_secs > prioritized_seconds:
        evidence.append(
            f"{_blocks(total_secs - prioritized_seconds, block_seconds)} "
            f"blocks on unprioritized commitments sat out of this score")
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


def _consistency(cycle: dict, sessions: list[SessionSlice],
                 today: date) -> dict:
    """active days over elapsed cycle days. the denominator stops at
    `today` so a cycle scored mid-window (closed early) is not blamed for
    days that never happened."""
    start = _parse_date(cycle.get("start_date"))
    end = _parse_date(cycle.get("end_date"))
    if start is None:
        return _component(None, ["the cycle has no start date; days cannot "
                                 "be counted"])
    last = min(end or today, today)
    elapsed = (last - start).days + 1
    if elapsed <= 0:
        return _component(None, ["the cycle window has no elapsed days yet"])
    active_days = sorted({s.local_date for s in sessions
                          if s.seconds > 0 and start <= s.local_date <= last})
    evidence = [f"worked {len(active_days)} of {elapsed} cycle days"]
    gap = _longest_gap(active_days, start, last)
    if gap > 1:
        evidence.append(f"longest quiet stretch: {gap} days")
    return _component(len(active_days) / elapsed, evidence)


def _sustainability(sessions: list[SessionSlice]) -> dict:
    """share of focus time OUTSIDE the late-night window [23:00, 06:00).
    v1 measures the one signal the ledger already carries honestly; burst
    detection and rest-day arithmetic can join once there is history."""
    total = sum(s.seconds for s in sessions if s.seconds > 0)
    if total <= 0:
        return _component(None, ["no focus time in the window; pace cannot "
                                 "be judged"])
    late = sum(s.seconds for s in sessions
               if s.seconds > 0 and _is_late(s.local_hour))
    share = late / total
    if late == 0:
        evidence = ["no focus time landed in the late-night hours "
                    "(23:00-06:00)"]
    else:
        evidence = [f"{round(share * 100)}% of focus time landed in the "
                    f"late-night hours (23:00-06:00)"]
    return _component(1.0 - share, evidence)


# --- shared arithmetic -------------------------------------------------------

def _numbers(totals: dict, sessions: list[SessionSlice], cycle: dict,
             today: date) -> dict:
    """the raw counts the components were derived from - kept beside the
    scores so an assessment is auditable without re-running anything."""
    total_secs = sum(s.seconds for s in sessions if s.seconds > 0)
    start = _parse_date(cycle.get("start_date"))
    end = _parse_date(cycle.get("end_date"))
    last = min(end or today, today)
    return {
        "planned_blocks": totals.get("planned_blocks"),
        "done_blocks": totals.get("done_blocks"),
        "capacity_blocks": totals.get("capacity_blocks"),
        "unattributed_seconds": totals.get("unattributed_seconds"),
        "session_count": sum(1 for s in sessions if s.seconds > 0),
        "session_seconds": total_secs,
        "active_days": len({s.local_date for s in sessions if s.seconds > 0}),
        "elapsed_days": ((last - start).days + 1
                         if start is not None and last >= start else None),
        "late_seconds": sum(s.seconds for s in sessions
                            if s.seconds > 0 and _is_late(s.local_hour)),
    }


def _is_late(hour: int) -> bool:
    return hour >= _LATE_START or hour < _LATE_END


def _longest_gap(active_days: list[date], start: date, last: date) -> int:
    """longest run of workless days inside [start, last], edges included."""
    if not active_days:
        return (last - start).days + 1
    gap = max((active_days[0] - start).days, (last - active_days[-1]).days)
    for earlier, later in zip(active_days, active_days[1:]):
        gap = max(gap, (later - earlier).days - 1)
    return gap


def _parse_date(value) -> Optional[date]:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
