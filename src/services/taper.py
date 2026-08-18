"""the taper: success extends the quiet (ROOMS_DESIGN.md section 7).

the arc's "rhythm" posture, as arithmetic. the scorecard ledger (phase
6a) is the evidence base; this module reads the recent cycle cards and
answers one question: how much quiet has this person EARNED? each
consecutive steady cycle doubles the check-in interval - the inverted
mirror of the backoff gate, which stretches the wait when outreach goes
unanswered. the two compose: the taper sets the beat, the backoff still
guards ignored chains on top of it.

the honesty rules, in the same key as the scorer's:

- quiet is earned on evidence. a cycle whose consistency or execution
  could not be computed (nothing planned, never frozen) is "unjudged" -
  it neither extends the quiet nor resets it, but it BREAKS the streak:
  absence of evidence earns nothing.
- a wobbly cycle resets to full coaching. the streak counts consecutive
  steady cycles from the most recent backwards, full stop.
- unsustainable bursts never earn quiet: a cycle that clears execution
  on late-night hours fails the steady test even with perfect numbers.
- presence never tapers to zero. the multiplier caps (the "keeping
  watch" posture - the sentinel keeps its post).

deliberately NOT here (deferred, not faked): per-routine nudges lost
"one by one" (exactly one proactive rhythm exists today - the check-in
beat - so the taper stretches that one), and edwin's retire-this-
tracking recommendations. both arrive when there are more nudges to
lose.
"""
from __future__ import annotations

from typing import Optional

from config import Config

# the arc's postures (the section 7 table), mapped to the arithmetic:
# no cards yet = settling in; streak zero = building (full coaching);
# any earned quiet = rhythm; at the cap = keeping watch.
SETTLING_IN = "settling in"
BUILDING = "building"
RHYTHM = "rhythm"
KEEPING_WATCH = "keeping watch"


def assess(assessments: list[dict]) -> dict:
    """the pure arithmetic: recent cycle assessments (detached rows, as
    cycle_scorer reads them) -> the taper decision. ordered here by the
    cycle's seal (not the card's filing - a backfilled old cycle scored
    late must not masquerade as the latest evidence)."""
    ordered = sorted(assessments, key=_sealed_key, reverse=True)
    ordered = ordered[:Config.TAPER_WINDOW]
    judged = [_judge(a) for a in ordered]

    streak = 0
    for j in judged:
        if j["verdict"] != "steady":
            break
        streak += 1
    multiplier = min(2 ** streak, Config.TAPER_MAX_MULTIPLIER)

    if not judged:
        posture = SETTLING_IN
    elif streak == 0:
        posture = BUILDING
    elif multiplier >= Config.TAPER_MAX_MULTIPLIER:
        posture = KEEPING_WATCH
    else:
        posture = RHYTHM
    return {
        "posture": posture,
        "streak": streak,
        "multiplier": multiplier,
        "judged": judged,
    }


def state_for(user_uuid: str, base_minutes: int) -> dict:
    """the decision for one user, with the interval it implies. the one
    db touch in this module."""
    from src.services import cycle_scorer
    rows = cycle_scorer.recent_assessments(
        user_uuid, subject_type="cycle", limit=Config.TAPER_WINDOW)
    decision = assess(rows)
    decision["base_minutes"] = int(base_minutes)
    decision["checkin_minutes"] = int(base_minutes) * decision["multiplier"]
    return decision


def checkin_minutes(user_uuid: str) -> int:
    """the pulse's read: the stretched check-in interval, in minutes."""
    return state_for(
        user_uuid, Config.DM_INTERVAL_MINUTES)["checkin_minutes"]


def ambient_line(decision: dict) -> Optional[str]:
    """the council's one-line briefing on the house's posture, only when
    quiet has actually been earned - an untapered house adds nothing (and
    a fresh user's prompt bytes stay exactly as they were). deterministic
    render of the decision; no model ever writes this."""
    if decision.get("multiplier", 1) <= 1:
        return None
    streak = decision["streak"]
    cycles = "cycle" if streak == 1 else "cycles"
    beat = _fmt_minutes(decision.get("checkin_minutes"))
    if decision["posture"] == KEEPING_WATCH:
        return (f"the house is keeping watch: {streak} steady {cycles} "
                f"have earned the full quiet - check-ins hold at about "
                f"every {beat}. proactive voice is reserved for real "
                "early-warning signals, surfaced gently.")
    return (f"the house is in rhythm: {streak} steady {cycles} have "
            f"earned longer quiet - check-ins stretched to about every "
            f"{beat}. keep proactive touches light; this quiet is earned.")


def ambient_line_for(user_uuid: str) -> Optional[str]:
    """the ambient composer's one call: read, decide, render."""
    return ambient_line(state_for(user_uuid, Config.DM_INTERVAL_MINUTES))


def _fmt_minutes(minutes) -> str:
    m = int(minutes or 0)
    if m and m % 60 == 0:
        h = m // 60
        return "1 hour" if h == 1 else f"{h} hours"
    return f"{m} minutes"


# --- the steady test ---------------------------------------------------------

def _judge(assessment: dict) -> dict:
    """one cycle's verdict: steady | wobbly | unjudged, with the reasons
    spelled out (the evidence lines the api and ambient read)."""
    detail = assessment.get("detail") or {}
    cycle = detail.get("cycle") or {}
    title = cycle.get("title") or assessment.get("subject_id") or "a cycle"
    consistency = _score(detail, "consistency")
    execution = _score(detail, "execution")
    sustainability = _score(detail, "sustainability")

    reasons: list[str] = []
    unjudged = False
    steady = True
    for name, score, floor in (
        ("consistency", consistency, Config.TAPER_STEADY_CONSISTENCY),
        ("execution", execution, Config.TAPER_STEADY_EXECUTION),
    ):
        if score is None:
            unjudged = True
            reasons.append(f"{name} could not be computed")
        elif score < floor:
            steady = False
            reasons.append(f"{name} {score:.2f} below {floor:.2f}")
        else:
            reasons.append(f"{name} {score:.2f}")
    # sustainability guards but never blanks the verdict: None means no
    # sessions at all, and consistency has already said so
    if sustainability is not None:
        if sustainability < Config.TAPER_MIN_SUSTAINABILITY:
            steady = False
            reasons.append(f"sustainability {sustainability:.2f} below "
                           f"{Config.TAPER_MIN_SUSTAINABILITY:.2f} - "
                           "bursts don't earn quiet")
        else:
            reasons.append(f"sustainability {sustainability:.2f}")

    verdict = "unjudged" if unjudged else ("steady" if steady else "wobbly")
    return {
        "subject_id": assessment.get("subject_id"),
        "title": title,
        "verdict": verdict,
        "reasons": reasons,
    }


def _score(detail: dict, name: str) -> Optional[float]:
    comp = (detail.get("components") or {}).get(name) or {}
    score = comp.get("score")
    return float(score) if isinstance(score, (int, float)) else None


def _sealed_key(assessment: dict) -> str:
    """iso strings order lexicographically; a row missing both stamps
    sorts oldest (it can't claim to be the latest evidence)."""
    detail = assessment.get("detail") or {}
    cycle = detail.get("cycle") or {}
    return cycle.get("sealed_at") or assessment.get("created_at") or ""
