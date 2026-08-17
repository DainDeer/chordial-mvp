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
ranked correction boundaries. weak evidence never earns the
recommendation - when the tail holds no strong run at all, the
recommendation falls back to the start of the block itself. candidates
are framed as REMOVAL amounts (impact, not inference); the engine
derives what the clock becomes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

# the authoritative numbers live in docs/REWIND_DESIGN.md section 11 -
# change the doc first
GAP_BREAK_SECONDS = 120.0        # a gap that splits activity runs
MICRO_MIN_MOMENTS = 3            # a run this thin is a jiggle, not work...
MICRO_MIN_SPAN_SECONDS = 60.0    # ...and so is one this short
MAX_CANDIDATES = 2               # surfaced; the list shape supports n

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
    """one correction boundary, ranked. `removed_seconds` is the impact
    (what choosing it excises, as of `now`); the offer layer derives the
    resulting clock. `kind` is "run_end" for an activity edge and
    "session_start" for the whole-block fallback; later kinds ("lock",
    "app_boundary") extend this vocabulary, not the shape."""
    at: datetime
    kind: str
    rank: int
    removed_seconds: int


class ActivityLedger:
    """the session-scoped moment history, reconstructed from (t, idle)
    samples. in-memory only - cleared when a run starts, dropped with the
    process, never persisted, never synced, never shown."""

    def __init__(self) -> None:
        self._moments: list[Moment] = []

    def observe(self, at: datetime, idle_seconds: float) -> bool:
        """one collector sample. appends an input-moment when the implied
        last-input time advanced; returns whether it did. a value that
        stands still (pure quiet) or steps backwards (clock weirdness,
        stale sample) adds nothing - the ledger only ever learns."""
        last_input = at - timedelta(seconds=max(0.0, float(idle_seconds)))
        if self._moments:
            advance = (last_input - self._moments[-1].t).total_seconds()
            if advance < _ADVANCE_EPSILON_SECONDS:
                return False
        self._moments.append(Moment(last_input, "input"))
        if len(self._moments) > _MOMENT_CAP:
            del self._moments[: len(self._moments) - _MOMENT_CAP]
        return True

    def clear(self) -> None:
        """a new run starts a new story."""
        self._moments = []

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


def _sessionize(moments: list[Moment], floor: datetime,
                gap_break_s: float) -> list[_Run]:
    """cluster presence-moments into maximal runs: a gap short enough to
    be a thinking pause never splits one. moments from before the run's
    floor are another story's tail and are clipped defensively."""
    runs: list[_Run] = []
    for m in sorted(moments, key=lambda m: m.t):
        if m.kind not in PRESENCE_KINDS or m.t < floor:
            continue
        if runs and (m.t - runs[-1].end).total_seconds() <= gap_break_s:
            last = runs[-1]
            runs[-1] = _Run(last.start, m.t, last.count + 1)
        else:
            runs.append(_Run(m.t, m.t, 1))
    return runs


def rewind_candidates(moments: list[Moment], now: datetime,
                      floor: datetime, *,
                      gap_break_s: float = GAP_BREAK_SECONDS,
                      max_candidates: int = MAX_CANDIDATES,
                      ) -> list[Candidate]:
    """the ranked correction boundaries for a quiet tail ending at `now`.
    `floor` is the run's start - the last moment that is CERTAINLY work
    (starting a block is an intentional act), and the fallback
    recommendation when the tail holds no strong run at all.

    rank 0 is the recommendation: the end of the most recent STRONG run
    (or the floor). trailing micro-runs between it and now become
    lower-ranked "if that blip was work" alternatives, most recent
    first. deterministic; pure; no side effects."""
    runs = _sessionize(moments, floor, gap_break_s)

    trailing_micro: list[_Run] = []
    primary_at, primary_kind = floor, "session_start"
    for run in reversed(runs):
        if run.strong:
            primary_at, primary_kind = run.end, "run_end"
            break
        trailing_micro.append(run)     # most recent first, by construction

    def _candidate(at: datetime, kind: str, rank: int) -> Candidate:
        removed = max(0, int((now - at).total_seconds()))
        return Candidate(at, kind, rank, removed)

    candidates = [_candidate(primary_at, primary_kind, 0)]
    for run in trailing_micro:
        if len(candidates) >= max_candidates:
            break
        candidates.append(_candidate(run.end, "run_end", len(candidates)))
    return candidates
