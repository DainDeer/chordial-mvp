"""rewind slice A (docs/REWIND_DESIGN.md sections 4-5): the ledger and
the question, on synthetic streams.

everything here is pure - no db, no sidecar process, no network. the
ledger tests feed collector-shaped (t, idle) samples and assert the
reconstructed input-moments; the candidate tests build moment streams
directly and assert the ranked boundaries. times are minutes past a
fixed 2pm so the scenarios read like the design doc's diagrams.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sidecar.rewind import (
    ActivityLedger,
    Moment,
    _MOMENT_CAP,
    rewind_candidates,
)

T0 = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def moments_every(start_min: float, end_min: float,
                  step_s: float = 10.0) -> list[Moment]:
    """a solid stretch of typing: input-moments every `step_s` seconds."""
    out, t = [], at(start_min)
    while t <= at(end_min):
        out.append(Moment(t, "input"))
        t += timedelta(seconds=step_s)
    return out


# --- the ledger: reconstruction, not collection ------------------------------


def test_ledger_reconstructs_last_input_from_idle():
    """a sample (t, idle) means the last input landed at exactly t - idle."""
    ledger = ActivityLedger()
    assert ledger.observe(at(1), idle_seconds=30) is True
    assert ledger.moments() == [Moment(at(1) - timedelta(seconds=30), "input")]


def test_pure_quiet_adds_nothing():
    """while nobody touches anything, idle grows in lockstep with t - the
    implied last-input stands still and the ledger learns nothing new."""
    ledger = ActivityLedger()
    ledger.observe(at(0), idle_seconds=0)
    for m in range(1, 6):
        assert ledger.observe(at(m), idle_seconds=60.0 * m) is False
    assert len(ledger.moments()) == 1


def test_activity_between_heartbeats_is_recovered():
    """input that lands between samples still gets its exact timestamp:
    the next sample's t - idle points straight at it."""
    ledger = ActivityLedger()
    ledger.observe(at(0), idle_seconds=0)
    # quiet for 4 minutes, then a keystroke at 4:30, sampled at 5:00
    assert ledger.observe(at(5), idle_seconds=30) is True
    assert ledger.moments()[-1].t == at(5) - timedelta(seconds=30)


def test_jitter_on_the_same_keystroke_dedupes():
    """two samples disagreeing by sub-second float noise describe one
    keystroke, not two moments."""
    ledger = ActivityLedger()
    ledger.observe(at(1), idle_seconds=10.0)
    assert ledger.observe(at(1.05), idle_seconds=12.6) is False   # ~0.4s later
    assert len(ledger.moments()) == 1


def test_a_backwards_sample_is_ignored():
    """a stale or clock-skewed sample implying an EARLIER last-input than
    we already know must not corrupt the story - the ledger only learns."""
    ledger = ActivityLedger()
    ledger.observe(at(5), idle_seconds=0)
    assert ledger.observe(at(6), idle_seconds=300) is False   # implies 1:00
    assert ledger.moments() == [Moment(at(5), "input")]


def test_clear_starts_a_new_story():
    ledger = ActivityLedger()
    ledger.observe(at(1), idle_seconds=0)
    ledger.clear()
    assert ledger.moments() == []


def test_the_cap_trims_the_old_end():
    """marathon sessions stay bounded, and it's the tail (what candidates
    read) that survives."""
    ledger = ActivityLedger()
    t = T0
    for _ in range(_MOMENT_CAP + 100):
        t += timedelta(seconds=2)
        ledger.observe(t, idle_seconds=0)
    kept = ledger.moments()
    assert len(kept) == _MOMENT_CAP
    assert kept[-1].t == t                     # newest moment intact


# --- the question: sessionization + ranked boundaries ------------------------


def test_the_docs_own_diagram():
    """the worked example from section 5: solid typing 2:00-2:14, two
    stray moments around 2:20, return at 2:31. A = the strong run's end
    (recommended), B = the blip's edge (disclosure)."""
    stream = moments_every(0, 14) + [Moment(at(20), "input"),
                                     Moment(at(20.05), "input")]
    got = rewind_candidates(stream, now=at(31), floor=at(0))
    assert [(c.kind, c.rank) for c in got] == [("run_end", 0), ("run_end", 1)]
    assert got[0].at == at(14)
    assert got[1].at == at(20.05)
    assert got[0].removed_seconds == 17 * 60
    assert got[1].removed_seconds == int((at(31) - at(20.05)).total_seconds())


def test_the_mouse_jiggle_is_not_a_comeback():
    """weak evidence never earns the recommendation - the jiggle ranks
    behind the real run no matter how recent it is."""
    stream = moments_every(0, 10) + [Moment(at(18), "input")]
    got = rewind_candidates(stream, now=at(25), floor=at(0))
    assert got[0].at == at(10) and got[0].rank == 0
    assert got[1].at == at(18) and got[1].rank == 1


def test_thinking_pauses_dont_split_the_run():
    """a 90s stare at the wall is part of working - the run survives it
    and the boundary lands at the run's true end, not the pause."""
    stream = (moments_every(0, 5)
              + moments_every(6.5, 12))       # 90s gap < gap_break (120s)
    got = rewind_candidates(stream, now=at(30), floor=at(0))
    assert len(got) == 1
    assert got[0].at == at(12)


def test_a_long_gap_does_split_the_run():
    """three minutes of quiet is two runs, and only the later one's edge
    is the boundary."""
    stream = moments_every(0, 5) + moments_every(8, 14)
    got = rewind_candidates(stream, now=at(30), floor=at(0))
    assert got[0].at == at(14)


def test_sleep_reads_as_one_long_gap():
    """v1 needs no lock/wake wiring: a closed laptop is just a very long
    gap, and the boundary lands on the last moment before the lid."""
    stream = moments_every(0, 20)
    got = rewind_candidates(stream, now=at(140), floor=at(0))   # 2h asleep
    assert got[0].at == at(20)
    assert got[0].removed_seconds == 120 * 60


def test_an_empty_tail_recommends_the_session_start():
    """no moments at all: the only certain engagement is the intentional
    act of starting the block."""
    got = rewind_candidates([], now=at(15), floor=at(0))
    assert [(c.kind, c.rank) for c in got] == [("session_start", 0)]
    assert got[0].at == at(0)
    assert got[0].removed_seconds == 15 * 60


def test_only_jiggles_still_recommend_the_session_start():
    """a session holding nothing but micro-runs has no strong evidence -
    rank 0 falls back to the floor, the freshest blip is the disclosure."""
    stream = [Moment(at(3), "input"), Moment(at(3.05), "input"),
              Moment(at(9), "input")]
    got = rewind_candidates(stream, now=at(15), floor=at(0))
    assert got[0].kind == "session_start" and got[0].at == at(0)
    assert got[1].kind == "run_end" and got[1].at == at(9)


def test_a_short_burst_is_not_strong_evidence():
    """mass needs BOTH enough moments and enough span: a dense 30-second
    burst is a blip by span, and must not earn the recommendation."""
    stream = moments_every(5, 5.5, step_s=2)      # ~16 moments over 30s
    got = rewind_candidates(stream, now=at(20), floor=at(0))
    assert got[0].kind == "session_start"
    assert got[1].at == at(5.5)


def test_at_most_two_candidates_surface():
    """recovery is never bookkeeping: many trailing blips still collapse
    to the recommendation plus one disclosure."""
    stream = moments_every(0, 10) + [Moment(at(13), "input"),
                                     Moment(at(16), "input"),
                                     Moment(at(19), "input")]
    got = rewind_candidates(stream, now=at(25), floor=at(0))
    assert len(got) == 2
    assert got[0].at == at(10)
    assert got[1].at == at(19)      # the most recent blip wins the slot


def test_future_context_kinds_dont_join_runs():
    """forward-compat: a non-presence kind (a lock event, an app switch)
    in the stream labels a boundary someday - today it must not
    manufacture activity."""
    stream = moments_every(0, 10) + [Moment(at(18), "lock")]
    got = rewind_candidates(stream, now=at(25), floor=at(0))
    assert len(got) == 1
    assert got[0].at == at(10)


def test_moments_before_the_floor_are_another_story():
    """the ledger is cleared on run start, but the function still clips
    defensively - a stale pre-run tail must never place a boundary
    before the block existed."""
    stream = moments_every(-10, -5) + moments_every(0, 8)
    got = rewind_candidates(stream, now=at(20), floor=at(0))
    assert len(got) == 1
    assert got[0].at == at(8)


def test_an_active_tail_yields_a_zero_removal():
    """called while the person is typing (no drift), the recommendation
    is the newest moment and removes ~nothing - the offer layer simply
    has nothing to ask about."""
    stream = moments_every(0, 10)
    got = rewind_candidates(stream, now=at(10), floor=at(0))
    assert got[0].at == at(10)
    assert got[0].removed_seconds == 0
