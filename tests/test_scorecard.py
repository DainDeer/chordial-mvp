"""the deterministic scorecard (phase 6, ROOMS_DESIGN.md section 8).

the arithmetic half of edwin's scorer: every component score is computed
from the projection and the session ledger, no model in the loop. the
invariants pinned here:

- a component that cannot be honestly computed says so (score None, the
  reason in evidence) instead of faking a zero or a one
- execution's overshoot clamps at 1.0 - doing more than planned is judged
  by estimation, never punished twice
- prioritization weighs only attributed time on prioritized commitments;
  unprioritized time is neither credit nor blame
- estimation exists only against a FROZEN baseline, and released
  commitments stay in (releasing moved scope honestly; the estimate was
  still an estimate)
- consistency's denominator stops at 'today', so a cycle scored mid-window
  is not blamed for days that never happened
- there is deliberately no overall grade anywhere in the shape
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.scorecard import (COMPONENTS, SessionSlice,
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


def _slice(day_offset, seconds=BLOCK, hour=14):
    return SessionSlice(seconds=seconds,
                        local_date=date.fromordinal(D.toordinal()
                                                    + day_offset),
                        local_hour=hour)


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


def test_prioritization_weighs_where_attributed_time_went():
    commitments = [
        {"uuid": "a", "priority": "high", "seconds_done": 3000},
        {"uuid": "b", "priority": "low", "seconds_done": 1500},
    ]
    comp = _card(_proj(commitments=commitments),
                 [])["components"]["prioritization"]
    # (3000*1.0 + 1500*0.4) / 4500 = 0.8
    assert comp["score"] == 0.8
    assert any("2.0 of 3.0 prioritized blocks" in e for e in comp["evidence"])


def test_unprioritized_time_is_neither_credit_nor_blame():
    commitments = [
        {"uuid": "a", "priority": "high", "seconds_done": 3000},
        {"uuid": "b", "priority": None, "seconds_done": 3000},
    ]
    comp = _card(_proj(commitments=commitments),
                 [])["components"]["prioritization"]
    assert comp["score"] == 1.0
    assert any("sat out" in e for e in comp["evidence"])


def test_prioritization_with_no_prioritized_time_scores_nothing():
    commitments = [{"uuid": "a", "priority": None, "seconds_done": 3000}]
    comp = _card(_proj(commitments=commitments),
                 [])["components"]["prioritization"]
    assert comp["score"] is None


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
    sessions = [_slice(d) for d in (0, 1, 3, 5, 7, 9, 11)]
    comp = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 13))
    comp = comp["components"]["consistency"]
    assert comp["score"] == 0.5
    assert "worked 7 of 14 cycle days" in comp["evidence"]


def test_consistency_names_the_longest_quiet_stretch():
    sessions = [_slice(0), _slice(1), _slice(13)]
    comp = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 13))
    comp = comp["components"]["consistency"]
    assert any("quiet stretch: 11 days" in e for e in comp["evidence"])


def test_a_cycle_scored_mid_window_is_not_blamed_for_unlived_days():
    # scored on day 7 of 14: the denominator is 7, not 14
    sessions = [_slice(d) for d in range(7)]
    comp = _card(_proj(days=14), sessions,
                 today=date.fromordinal(D.toordinal() + 6))
    comp = comp["components"]["consistency"]
    assert comp["score"] == 1.0


# --- sustainability ----------------------------------------------------------


def test_sustainability_is_the_daylight_share():
    sessions = [_slice(0, seconds=4500, hour=14),
                _slice(1, seconds=1500, hour=23)]
    comp = _card(_proj(), sessions)["components"]["sustainability"]
    assert comp["score"] == 0.75
    assert any("25% of focus time" in e for e in comp["evidence"])


def test_the_late_window_edges_are_exact():
    # 23:00 is late, 05:00 is late, 06:00 is not
    sessions = [_slice(0, hour=23), _slice(1, hour=5), _slice(2, hour=6)]
    comp = _card(_proj(), sessions)["components"]["sustainability"]
    assert comp["score"] == round(1 - 2 / 3, 2)


def test_a_quiet_cycle_cannot_have_its_pace_judged():
    comp = _card(_proj(), [])["components"]["sustainability"]
    assert comp["score"] is None


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
                 [_slice(0)])
    assert set(card.keys()) == {"components", "numbers"}
    assert set(card["components"].keys()) == set(COMPONENTS)


def test_the_numbers_are_kept_beside_the_scores():
    sessions = [_slice(0, seconds=3000, hour=23), _slice(2, seconds=1500)]
    card = _card(_proj(totals={"planned_blocks": 6, "done_blocks": 3.0},
                       days=14),
                 sessions, today=date.fromordinal(D.toordinal() + 13))
    numbers = card["numbers"]
    assert numbers["session_count"] == 2
    assert numbers["session_seconds"] == 4500
    assert numbers["active_days"] == 2
    assert numbers["elapsed_days"] == 14
    assert numbers["late_seconds"] == 3000
