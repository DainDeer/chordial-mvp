"""the deterministic scorecard (phase 6, ROOMS_DESIGN.md section 8).

the arithmetic half of edwin's scorer: every component score is computed
from the projection and the session ledger, no model in the loop. the
invariants pinned here:

- a component that cannot be honestly computed says so (score None, the
  reason in evidence) instead of faking a zero or a one
- execution's overshoot clamps at 1.0 - doing more than planned is judged
  by estimation, never punished twice
- sessions are judged over their WHOLE user-local interval: split at
  midnight for day counting, split at the 23:00/06:00 edges for
  sustainability, clipped to the cycle window so a boundary-straddling
  run contributes exactly its inside part
- prioritization weighs attributed time against the priorities AS FROZEN
  in the baseline - a late priority edit must not recast earlier blocks -
  and unfrozen cycles have no fixed reference to judge against
- estimation exists only against a FROZEN baseline, and released
  commitments stay in (releasing moved scope honestly; the estimate was
  still an estimate)
- consistency's denominator stops at 'today' (the seal date), so a cycle
  closed early is not blamed for days that never happened
- there is deliberately no overall grade anywhere in the shape
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.scorecard import (COMPONENTS, SessionSpan,
                                    compute_scorecard)

BLOCK = 1500                       # 25-minute blocks
D = date(2026, 8, 10)              # cycle start, a monday


def _proj(commitments=None, totals=None, frozen=False, start=D, days=14):
    end = date.fromordinal(start.toordinal() + days - 1)
    return {
        "cycle": {"title": "cycle t", "theme": "steady",
                  "start_date": start.isoformat(),
                  "end_date": end.isoformat()},
        "frozen": frozen,
        "commitments": commitments or [],
        "scope_changes": [],
        "totals": totals or {},
    }


def _span(day_offset, start_hm=(14, 0), minutes=25):
    """a session interval starting `day_offset` days into the cycle."""
    start = datetime.combine(
        date.fromordinal(D.toordinal() + day_offset), time(*start_hm))
    return SessionSpan(start=start, end=start + timedelta(minutes=minutes))


def _card(projection, sessions, today=None):
    return compute_scorecard(projection, sessions,
                             today=today or date(2026, 8, 24),
                             block_seconds=BLOCK)


# --- execution ---------------------------------------------------------------


def test_execution_is_the_landed_share_of_the_plan():
    card = _card(_proj(totals={"planned_blocks": 14, "done_blocks": 11.0}),
                 [])
    comp = card["components"]["execution"]
    assert comp["score"] == 0.79
    assert "11.0 of 14 planned blocks landed" in comp["evidence"]


def test_execution_overshoot_clamps_and_points_at_estimation():
    card = _card(_proj(totals={"planned_blocks": 4, "done_blocks": 6.2}), [])
    comp = card["components"]["execution"]
    assert comp["score"] == 1.0
    assert any("estimation" in e for e in comp["evidence"])


def test_execution_with_no_plan_scores_nothing():
    card = _card(_proj(totals={"planned_blocks": 0, "done_blocks": 3.0}), [])
    assert card["components"]["execution"]["score"] is None


# --- prioritization ----------------------------------------------------------


def test_prioritization_weighs_time_against_the_frozen_priorities():
    commitments = [
        {"uuid": "a", "baseline_priority": "high", "priority": "high",
         "seconds_done": 3000},
        {"uuid": "b", "baseline_priority": "low", "priority": "low",
         "seconds_done": 1500},
    ]
    comp = _card(_proj(commitments=commitments, frozen=True),
                 [])["components"]["prioritization"]
    # (3000*1.0 + 1500*0.4) / 4500 = 0.8
    assert comp["score"] == 0.8
    assert any("2.0 of 3.0 prioritized blocks" in e for e in comp["evidence"])


def test_a_late_priority_edit_cannot_recast_earlier_blocks():
    # frozen low, flipped to high afterward: the frozen weight judges it
    commitments = [
        {"uuid": "a", "baseline_priority": "low", "priority": "high",
         "seconds_done": 3000},
    ]
    comp = _card(_proj(commitments=commitments, frozen=True),
                 [])["components"]["prioritization"]
    assert comp["score"] == 0.4


def test_time_with_no_frozen_priority_is_neither_credit_nor_blame():
    # unprioritized at the freeze, and a post-freeze addition: both sit out
    commitments = [
        {"uuid": "a", "baseline_priority": "high", "priority": "high",
         "seconds_done": 3000},
        {"uuid": "b", "baseline_priority": None, "priority": "high",
         "seconds_done": 3000},
    ]
    comp = _card(_proj(commitments=commitments, frozen=True),
                 [])["components"]["prioritization"]
    assert comp["score"] == 1.0
    assert any("sat out" in e for e in comp["evidence"])


def test_prioritization_with_no_frozen_prioritized_time_scores_nothing():
    commitments = [{"uuid": "a", "baseline_priority": None,
                    "priority": "high", "seconds_done": 3000}]
    comp = _card(_proj(commitments=commitments, frozen=True),
                 [])["components"]["prioritization"]
    assert comp["score"] is None


def test_an_unfrozen_cycle_has_no_priority_reference():
    commitments = [{"uuid": "a", "baseline_priority": None,
                    "priority": "high", "seconds_done": 3000}]
    comp = _card(_proj(commitments=commitments, frozen=False),
                 [])["components"]["prioritization"]
    assert comp["score"] is None
    assert any("never frozen" in e for e in comp["evidence"])


# --- estimation --------------------------------------------------------------


def test_estimation_needs_a_frozen_baseline():
    commitments = [{"uuid": "a", "baseline_blocks": 5, "blocks_done": 5.0}]
    comp = _card(_proj(commitments=commitments, frozen=False),
                 [])["components"]["estimation"]
    assert comp["score"] is None
    assert any("frozen" in e for e in comp["evidence"])


def test_estimation_averages_per_commitment_accuracy():
    commitments = [
        {"uuid": "a", "title": "event pipeline", "status": "active",
         "baseline_blocks": 4, "blocks_done": 2.0},
        {"uuid": "b", "title": "steady one", "status": "completed",
         "baseline_blocks": 5, "blocks_done": 5.0},
    ]
    comp = _card(_proj(commitments=commitments, frozen=True),
                 [])["components"]["estimation"]
    # accuracies: 1-0.5=0.5 and 1.0 -> mean 0.75
    assert comp["score"] == 0.75
    assert any("'event pipeline' planned 4, landed 2.0" in e
               for e in comp["evidence"])


def test_released_commitments_still_count_as_estimates():
    commitments = [
        {"uuid": "a", "title": "dropped one", "status": "released",
         "baseline_blocks": 6, "blocks_done": 0.0},
    ]
    comp = _card(_proj(commitments=commitments, frozen=True),
                 [])["components"]["estimation"]
    assert comp["score"] == 0.0
    assert any("(released)" in e for e in comp["evidence"])


# --- consistency -------------------------------------------------------------


def test_consistency_counts_active_days_over_elapsed_days():
    sessions = [_span(d) for d in (0, 1, 3, 5, 7, 9, 11)]
    comp = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 13))
    comp = comp["components"]["consistency"]
    assert comp["score"] == 0.5
    assert "worked 7 of 14 cycle days" in comp["evidence"]


def test_consistency_names_the_longest_quiet_stretch():
    sessions = [_span(0), _span(1), _span(13)]
    comp = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 13))
    comp = comp["components"]["consistency"]
    assert any("quiet stretch: 11 days" in e for e in comp["evidence"])


def test_a_cycle_sealed_mid_window_is_not_blamed_for_unlived_days():
    # sealed on day 7 of 14: the denominator is 7, not 14
    sessions = [_span(d) for d in range(7)]
    comp = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 6))
    comp = comp["components"]["consistency"]
    assert comp["score"] == 1.0


def test_a_cross_midnight_session_is_work_on_both_days():
    # 23:30 day 0 -> 00:30 day 1: two active days, and every minute late
    sessions = [_span(0, (23, 30), 60)]
    card = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 13))
    assert card["numbers"]["active_days"] == 2
    assert card["components"]["sustainability"]["score"] == 0.0


# --- sustainability ----------------------------------------------------------


def test_sustainability_is_the_daylight_share():
    sessions = [_span(0, (14, 0), 75), _span(1, (23, 0), 25)]
    comp = _card(_proj(), sessions)["components"]["sustainability"]
    assert comp["score"] == 0.75
    assert any("25% of focus time" in e for e in comp["evidence"])


def test_a_session_is_judged_over_its_whole_interval():
    # sol's case: ends 06:00, ran 05:35-06:00 - every second was late
    comp = _card(_proj(), [_span(0, (5, 35), 25)])
    assert comp["components"]["sustainability"]["score"] == 0.0


def test_a_session_straddling_the_late_edge_splits_there():
    # 05:30-06:30: half late, half daylight
    comp = _card(_proj(), [_span(0, (5, 30), 60)])
    assert comp["components"]["sustainability"]["score"] == 0.5


def test_a_quiet_cycle_cannot_have_its_pace_judged():
    comp = _card(_proj(), [])["components"]["sustainability"]
    assert comp["score"] is None


# --- the window --------------------------------------------------------------


def test_a_boundary_straddling_session_contributes_its_inside_part():
    # ends 00:20 the day AFTER the cycle: only the 23:50-00:00 tail counts
    sessions = [_span(13, (23, 50), 30)]
    card = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 13))
    assert card["numbers"]["session_seconds"] == 600
    assert card["numbers"]["active_days"] == 1
    assert card["numbers"]["late_seconds"] == 600


def test_a_session_entirely_outside_the_window_is_invisible():
    sessions = [_span(20)]
    card = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 13))
    assert card["numbers"]["session_count"] == 0
    assert card["components"]["sustainability"]["score"] is None


# --- the shape ---------------------------------------------------------------


def test_an_empty_undated_cycle_scores_nothing_and_says_why():
    proj = _proj()
    proj["cycle"]["start_date"] = None
    proj["cycle"]["end_date"] = None
    card = _card(proj, [])
    for name in COMPONENTS:
        comp = card["components"][name]
        assert comp["score"] is None, name
        assert comp["evidence"], name


def test_an_empty_dated_cycle_is_honestly_inconsistent():
    # a lived window with zero active days is a measurement, not missing
    # data: consistency is 0.0, while the data-starved components stay None
    card = _card(_proj(days=14), [],
                 today=date.fromordinal(D.toordinal() + 13))
    assert card["components"]["consistency"]["score"] == 0.0
    assert card["components"]["sustainability"]["score"] is None


def test_there_is_no_overall_grade():
    card = _card(_proj(totals={"planned_blocks": 4, "done_blocks": 4.0}),
                 [_span(0)])
    assert set(card.keys()) == {"components", "numbers"}
    assert set(card["components"].keys()) == set(COMPONENTS)


def test_the_numbers_are_kept_beside_the_scores():
    sessions = [_span(0, (23, 0), 50), _span(2, (14, 0), 25)]
    card = _card(_proj(totals={"planned_blocks": 6, "done_blocks": 3.0},
                       days=14),
                 sessions, today=date.fromordinal(D.toordinal() + 13))
    numbers = card["numbers"]
    assert numbers["session_count"] == 2
    assert numbers["session_seconds"] == 4500
    assert numbers["active_days"] == 2
    assert numbers["elapsed_days"] == 14
    assert numbers["late_seconds"] == 3000
