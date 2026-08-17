"""rewind, slice A (docs/REWIND_DESIGN.md sections 4-5): the activity
ledger and the candidate question. pure - no engine wiring, no
persistence, no network; slice B hangs the offer machine off these.

the ledger RECONSTRUCTS input-moments from the collector heartbeat the
sidecar already receives: a sample (t, idle) means the last input landed
at exactly t - idle, so a moment is appended whenever that value
advances. no new signal is collected, nothing is persisted, nothing ever
syncs - moments live in process memory for the life of one run. moments
are TYPED from day one ("input" is v1's whole vocabulary) so app-context
kinds later join the stream without a rewrite.

rewind_candidates() is the question: sessionize the moments with
hysteresis (a short gap doesn't split a run of work; a mouse jiggle is
not a comeback), then offer the right edges of the trailing runs as
ranked correction boundaries. two contracts keep the question honest
(sol's slice-A review):

- the ledger is a WITNESS, not a judge: the whole-block fallback is only
  recommended when coverage provably reaches back to the run's start. a
  ledger that restarted mid-run prefers the edges it observed - or asks
  nothing at all - rather than indicting time it never saw.
- candidates carry BOUNDARIES, never amounts: the impact of a correction
  ("remove 17m") is computed at ask time and again at apply time via
  removal_impact(), so a question answered late still states today's
  truth, not the truth from when the drift was first noticed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple, Optional

# the authoritative numbers live in docs/REWIND_DESIGN.md section 11 -
# change the doc first
GAP_BREAK_SECONDS = 120.0        # a gap that splits activity runs
MICRO_MIN_MOMENTS = 3            # a run this thin is a jiggle, not work...
MICRO_MIN_SPAN_SECONDS = 60.0    # ...and so is one this short
MAX_CANDIDATES = 2               # surfaced; the list shape supports n
# how late the ledger's witness may begin and still count as having
# covered the run from its start (the first collector sample lands
# within ~10s of a run starting; anything later means we missed history)
COVERAGE_SLACK_SECONDS = 30.0

# moment kinds that count as being-present for sessionization. later
# kinds ("foreground", "lock", "wake") are context, not presence - they
# label boundaries without joining runs.
PRESENCE_KINDS = frozenset({"input"})

# a reconstructed last-input under this much past the newest moment is
# float jitter on the same keystroke, not new evidence
_ADVANCE_EPSILON_SECONDS = 1.0
# a few hours of 2s-cadence moments is ~7k rows; the cap is a memory
# guard for marathon sessions, trimming the OLD end (candidates only
# ever read the tail)
_MOMENT_CAP = 20_000


class Moment(NamedTuple):
    t: datetime
    kind: str


class Candidate(NamedTuple):
    """one correction boundary, ranked. deliberately NO amount here -
    boundaries persist, impacts don't; derive the removal with
    removal_impact() at the moment of asking or applying. `kind` is
    "run_end" for an activity edge and "session_start" for the
    whole-block fallback; later kinds ("lock", "app_boundary") extend
    this vocabulary, not the shape."""
    at: datetime
    kind: str
    rank: int


def removal_impact(boundary: datetime, upto: datetime) -> int:
    """the seconds an excision [boundary, upto] removes. computed at ASK
    time (the card renders it) and AGAIN at APPLY time (the excision uses
    it) - never persisted, so a candidate minted when drift was detected
    still states the truth after twenty more quiet minutes."""
    return max(0, int((upto - boundary).total_seconds()))


class ActivityLedger:
    """the session-scoped moment history, reconstructed from (t, idle)
    samples. in-memory only - cleared when a run starts, dropped with the
    process, never persisted, never synced, never shown.

    `covers_from` is the witness stand: the earliest instant this ledger
    can speak for. the first sample (t, idle) proves quiet across
    [t - idle, t], so coverage starts at t - idle - which means the os
    idle counter carries the witness ACROSS a sidecar restart when the
    whole gap was quiet, while a restart after real activity honestly
    reports that the ledger missed history."""

    def __init__(self) -> None:
        self._moments: list[Moment] = []
        self._covers_from: Optional[datetime] = None

    @property
    def covers_from(self) -> Optional[datetime]:
        return self._covers_from

    def observe(self, at: datetime, idle_seconds: float) -> bool:
        """one collector sample. appends an input-moment when the implied
        last-input time advanced; returns whether it did. a value that
        stands still (pure quiet) or steps backwards (clock weirdness,
        stale sample) adds nothing - the ledger only ever learns."""
        last_input = at - timedelta(seconds=max(0.0, float(idle_seconds)))
        if self._covers_from is None:
            self._covers_from = last_input
        if self._moments:
            advance = (last_input - self._moments[-1].t).total_seconds()
            if advance < _ADVANCE_EPSILON_SECONDS:
                return False
        self._moments.append(Moment(last_input, "input"))
        if len(self._moments) > _MOMENT_CAP:
            del self._moments[: len(self._moments) - _MOMENT_CAP]
        return True

    def clear(self) -> None:
        """a new run starts a new story - and a new witness stand."""
        self._moments = []
        self._covers_from = None

    def moments(self) -> list[Moment]:
        return list(self._moments)


class _Run(NamedTuple):
    start: datetime
    end: datetime
    count: int

    @property
    def strong(self) -> bool:
        """enough mass to be evidence of work: a jiggle (too few moments)
        or a blip (too short a span) never earns the recommendation."""
        span = (self.end - self.start).total_seconds()
        return (self.count >= MICRO_MIN_MOMENTS
                and span >= MICRO_MIN_SPAN_SECONDS)


def _sessionize(moments: list[Moment], floor: datetime, now: datetime,
                gap_break_s: float) -> list[_Run]:
    """cluster presence-moments into maximal runs: a gap short enough to
    be a thinking pause never splits one. moments outside [floor, now]
    are clipped - a pre-run tail is another story, and a moment from the
    future (clock rollback, malformed sample) must never place a
    boundary past `now` and reverse an excision interval."""
    runs: list[_Run] = []
    for m in sorted(moments, key=lambda m: m.t):
        if m.kind not in PRESENCE_KINDS or m.t < floor or m.t > now:
            continue
        if runs and (m.t - runs[-1].end).total_seconds() <= gap_break_s:
            last = runs[-1]
            runs[-1] = _Run(last.start, m.t, last.count + 1)
        else:
            runs.append(_Run(m.t, m.t, 1))
    return runs


def rewind_candidates(moments: list[Moment], now: datetime,
                      floor: datetime, *,
                      covered_from: Optional[datetime] = None,
                      gap_break_s: float = GAP_BREAK_SECONDS,
                      max_candidates: int = MAX_CANDIDATES,
                      ) -> list[Candidate]:
    """the ranked correction boundaries for a quiet tail ending at `now`.
    `floor` is the run's start; `covered_from` is the ledger's witness
    stand (ActivityLedger.covers_from) - pass it always, because the
    whole-block fallback hinges on it.

    rank 0 is the recommendation: the end of the most recent STRONG run.
    trailing micro-runs between it and now become lower-ranked "if that
    blip was work" alternatives, most recent first. when the tail holds
    no strong run at all, the fallback depends on the witness:

    - coverage reaches the floor -> the session start itself (starting a
      block is an intentional act, the last CERTAIN engagement)
    - coverage begins mid-run -> the ledger must not indict time it
      never saw: the most recent observed edge instead, and with no
      observed edges at all, NO candidates - the offer layer then has
      nothing to ask, which is the safe answer.

    candidates are boundaries only; derive amounts with removal_impact()
    at ask/apply time. deterministic; pure; no side effects."""
    runs = _sessionize(moments, floor, now, gap_break_s)

    trailing_micro: list[_Run] = []
    strong_end: Optional[datetime] = None
    for run in reversed(runs):
        if run.strong:
            strong_end = run.end
            break
        trailing_micro.append(run)     # most recent first, by construction

    covered = (covered_from is not None
               and (covered_from - floor).total_seconds()
               <= COVERAGE_SLACK_SECONDS)

    boundaries: list[tuple[datetime, str]] = []
    if strong_end is not None:
        boundaries.append((strong_end, "run_end"))
    elif covered:
        boundaries.append((floor, "session_start"))
    boundaries.extend((run.end, "run_end") for run in trailing_micro)

    return [Candidate(at, kind, rank)
            for rank, (at, kind) in enumerate(boundaries[:max_candidates])]
