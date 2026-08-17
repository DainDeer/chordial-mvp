# chordial: rewind (the retroactive pause)

*designed 2026-08-17 · revised same day after sol's design review (lifecycle,
deferral, undo, copy, data minimization), sol's slice-A code review on PR
#70 (the witness rule, impact at resolution time, freeze-don't-refuse), and
sol's slice-B code review on PR #71 (segment union, active-run resolution
ownership, return-aware hydration, next-offer handoff, live clock promise) ·
builds on docs/ROOMS_DESIGN.md §5 (presence & focus) and §6 (the honesty
model) · status: design locked, slices A + B built (the v1 ship line);
slice C (the tether door) remains*

## 1. what this is

You start a block. Fourteen minutes in, the thread slips — you space out, or
the doorbell rings, or a Slack fire needs putting out. The clock doesn't know;
it keeps counting. Twenty minutes later you're back, and the bar now claims 34
minutes of focus when the truth is 14.

**Rewind is the retroactive pause button, for people who will never remember
to press pause.** When you come back from a drift, chordial offers to remove
the quiet stretch from the clock — usually one recommended correction,
occasionally two when the signal is genuinely ambiguous. You tap, the dead
air is excised, the bar tells the truth again. Or you keep all the time —
equally valid, because keyboard quiet is *not* proof that work stopped.

This matters because the whole downstream spine (cycle projections, pip's
noticings, edwin's eventual planning-accuracy signal) is only as good as the
banked seconds feeding it. A clock that silently counts absence poisons the
honest data the arc depends on.

### decisions locked 2026-08-17 (don't re-litigate)

- **idle-only signals in v1**, but the ledger/candidate design must accept
  app-context signals later without a rewrite (typed entries, labeled
  candidates from day one)
- **no manual scrubber in v1** — the offer is the only door. (A
  "different time…" escape hatch is acknowledged future work — idle can't
  distinguish interruption from paper work, and the candidate can only ever
  be an input boundary — but it can wait *because* v1 has undo and never
  destroys a pending correction on pause.)
- **the ding re-arms**: a correction that takes credited time back under the
  block target lets the target-crossing announce again — the ding celebrates
  *credited* time
- **interruption time is never auto-credited** — but labeled candidates let
  the person choose a post-interruption boundary themselves (later, with
  app context)
- **telegram/discord is the fallback surface** when the desktop offer can't
  reach you (you never came back to the desk) — same offer, second door,
  **separately opted into**
- **windows and mac are both first-class** — everything above the collector
  is platform-agnostic; the collector grows a windows idle source

### from sol's review (2026-08-17, applied throughout)

- **pause/finish must never bank unreviewed time** — the most natural act on
  discovering a runaway timer is pressing pause, and lapsing the offer there
  would permanently bank the lie; stopping transitions now *resolve* the
  correction instead
- **defer, don't expire** — the card collapses to a quiet chip that lives
  until the run's natural stopping point; a correction is never destroyed by
  being ignored
- **impact language, not inference language** — buttons say what they do to
  the clock ("remove 17m"), never ask the person to interpret the detector
  or defend their behavior ("i was working" is out; "keep all time" is in)
- **undo** — suggested boundaries can be wrong and taps can be accidental;
  excisions are per-offer *intervals*, reversible
- **data minimization** — candidate timestamps stay on-device in v1; they
  travel only to deliver an opted-in tether ping
- **one gentle surface per episode** — the card carries the welcome; pip
  does not comment on corrections

### from sol's slice-A code review (PR #70, applied)

- **the ledger is a witness, not a judge** — the whole-block fallback is
  recommended only when the ledger's coverage provably reaches the run's
  start; a ledger restarted mid-run prefers the edges it observed, or asks
  nothing at all
- **boundaries persist, amounts don't** — a candidate carries no
  `removed_seconds`; the impact is computed at ask time and again at apply
  time, so a question answered late states today's truth
- **a pause attempt always stops the clock** — the safety net for an
  unresolved offer *freezes* the run unbanked instead of refusing the
  transition; a guard must never keep a runaway timer running
- **moments outside `[floor, now]` are clipped** — a future moment (clock
  rollback, malformed sample) can never place a boundary past `now` and
  reverse an excision interval

### from sol's slice-B code review (PR #71, applied)

- **the question accumulates its gaps** — extension moves each returned
  episode's interval into `segments`; remove excises the union, so a
  second drift can never silently credit the first unresolved gap
- **transition resolutions belong to the active run** — a held run's
  question resolves only on the card (which banks it); riding it on
  another run's pause would orphan the frozen run unbanked forever
- **hydration respects a recorded return** — only an unreturned offer
  re-arms the drift episode on boot, and a recorded return is never
  overwritten
- **resolution hands back the next question** — the response carries the
  next authoritative open offer (another held block may be waiting), so
  no card is lost to a broadcast race
- **the clock promise is live** — "clock becomes 14:00" derives from the
  ticking local clock for an active offer; only a frozen offer uses the
  server's fixed value

## 2. principles

1. **Offer, never auto.** The machine cannot distinguish spacing out from
   thinking hard from reading on paper. Rewind is always a question;
   "keep all time" is a first-class answer presented as an equal, not a
   confession. Quiet at the desk is an *observation*, never an accusation.
2. **A correction is never destroyed silently.** Not by time passing, not by
   pausing, not by finishing, not by switching tasks. It resolves — by the
   person's explicit choice, at the moment they act — or the run cannot
   quietly bank. The one exception is the generous direction: a resolution
   the surface genuinely cannot ask for defaults to keeping all time.
3. **Both truths are recorded.** Credited seconds are what bank — that's
   what attribution and the projection read. But the event stream carries
   `gross_seconds` and the excision trail too. Nothing is laundered; the
   drift stays visible as *data, not a failure* (the scope-change ethic,
   applied to minutes).
4. **Raw signals never leave the device.** The activity ledger is in-memory,
   session-scoped, never persisted, never synced. Candidate timestamps stay
   local in v1; what goes upstream is the coarse outcome — the gap's length,
   the choice made, the seconds excised.
5. **Live runs only.** Rewind operates on the running clock, before
   `session.ended` fires. "Every run banks exactly once" is untouched; a
   banked run is immutable, and post-hoc correction is a different feature
   we deliberately do not build.
6. **One unresolved correction per run.** A second drift while a correction
   is pending *extends* the open offer (candidates recomputed over the whole
   contested span) rather than stacking a second question. Recovery must
   never become bookkeeping.
7. **The return is the win.** The voice around rewind welcomes back, never
   mourns the gap — and the feedback is consolidated: one card, which *is*
   the welcome. No separate confirmation line, no pip commentary, no pile of
   gentle messages that adds up to conspicuous.

## 3. what already exists

Rewind is mostly assembled from parts that shipped in phases 4–5:

- **the collector** (`app/src-tauri/src/collector.rs`) posts
  `{bundle_id, idle_seconds}` every 2s (10s heartbeat when unchanged) to the
  sidecar — the privacy boundary, sanitized rust-side
- **`ActivityState`** (`src/sidecar/drift.py`) holds the latest sample with
  staleness honesty; **`DriftWatch`** already runs the episode machine:
  10 idle minutes mid-block → `drift.detected` + one soft check-in; activity
  again → `return.detected` + one welcome
- **the focus engine** (`src/sidecar/focus.py`) does wall-time arithmetic on
  stored timestamps — offline-safe, restart-safe
- **the outbox/sync contract** delivers device events exactly-once; the pump
  (`src/sidecar/sync.py`) already wakes every 15s
- **the focus-flow processor** (server) turns applied device events into
  pip's observations

What's new: an activity **ledger** (history, not just the latest sample), a
**candidate** function over it, an **offer** state machine hanging off the
existing drift episode, per-offer **excision intervals** on runs, and the
two **surfaces** (deer card/chip, tether ping).

## 4. the activity ledger

`ActivityState` keeps one sample; rewind needs a short history. The
**ledger** is a session-scoped, in-memory list of typed moments:

```
{"t": <utc datetime>, "kind": "input"}
```

**Reconstruction, not collection.** The collector already tells us everything:
a sample `(t, idle)` means the last input landed at exactly `t − idle`. The
ledger appends an input-moment whenever that value advances — during activity
(idle ≈ 0 each sample) that's a moment every heartbeat; during quiet, nothing.
No new signal is gathered; the ledger is derived from the heartbeat we
already have, at 2–10s resolution, which is plenty for gaps measured in
minutes.

**Extensibility (locked decision):** `kind` is typed from day one. v1 knows
only `"input"`. Later kinds slot in without touching the sessionizer's shape:

| future kind | source | role |
|---|---|---|
| `"foreground"` | bundle-id change (collector already sends it) | context boundaries → labeled candidates ("you were in slack until 2:26") |
| `"lock"` / `"wake"` | os session notifications | hard work-ended boundaries |

The sessionizer clusters moments whose kind is in a *presence set* (v1:
`{"input"}`); a later labeling pass maps context kinds onto candidate labels.
Adding app context = new kinds + a labeler, zero rewrite.

**Lifecycle:** cleared when a run starts, capped (a few hours of moments is
kilobytes), dropped on sidecar restart — offers survive restarts (§6), the
ledger doesn't need to. Never persisted, never synced, never shown.

**Sleep is free.** On both platforms the idle counter keeps climbing through
sleep/lock (no input arrives), so a closed laptop reads as one long gap — the
strongest "work ended here" signal — with no extra wiring in v1.

## 5. the candidates

The pure core — the bit that is, admittedly, a coding interview question:

```python
def rewind_candidates(moments, now, *, gap_break_s, micro_mass) -> list[Candidate]
```

**Sessionize with hysteresis.** Cluster input-moments into *runs*: a gap
shorter than `gap_break_s` (default **120s**) never splits a run — pauses to
think are part of working. Each run has a *mass* (duration and moment count);
a run below `micro_mass` (default: under 3 moments or under 60s) is a
*micro-run* — a mouse jiggle, a clock check — weak evidence of work.

**Scan the tail backwards from `now`.** Every run's right edge is a possible
correction boundary. The recommendation is the **end of the last strong
run**. A trailing micro-run between it and now yields a second, lower-ranked
candidate — the algorithm showing its uncertainty instead of guessing:

```
2:00 ──■■■■■■■■■■■■──╌╌╌╌──■──╌╌╌╌╌╌╌╌╌── 2:31  (return detected)
        typing, solid       two moments
        until 2:14          at ~2:20

candidates:
  A  remove 17m  (quiet since 2:14)       ← recommended (rank 0)
  B  remove 11m  (quiet since 2:20 — if that blip was work)   (rank 1)
```

**The candidate struct** (labels and kinds from day one — the extensibility
decision). A candidate is a **boundary, never an amount**:

```
{
  "at": <utc iso>,              # the boundary (mechanics; stays on-device in v1)
  "kind": "run_end",            # or "session_start"; later: "lock", "app_boundary"
  "rank": 0,
}
```

The *displayed* framing is still impact ("remove 17m · clock becomes
14:00") — but those numbers are **derived at ask time and again at apply
time** via `removal_impact(boundary, upto)`, never persisted (sol's slice-A
review): a candidate minted when drift was detected at ten quiet minutes
must not still say "remove 10m" when the person returns at thirty. The
card recomputes on render; the excision recomputes on application.

**When the tail holds no strong run at all** (the block was quiet from the
start, save perhaps a jiggle), weak evidence must not earn the
recommendation — but the fallback needs a **witness** (sol's slice-A
review). The ledger tracks `covers_from`, the earliest instant it can speak
for — its first sample `(t, idle)` proves quiet across `[t − idle, t]`, so
the OS idle counter even carries the witness across a sidecar restart when
the whole gap was quiet. Then:

- **coverage reaches the run's start** → rank 0 falls back to the block's
  start (`kind: "session_start"` — starting a block is an intentional act,
  the last *certain* engagement), most recent micro edge as the disclosure
- **coverage begins mid-run** (the sidecar restarted after real work the
  ledger never saw) → the fallback is **suppressed**: the most recent
  *observed* edge is recommended instead, and with nothing observed at all
  there are **no candidates** — the offer layer has nothing to ask, which
  is the safe answer. The ledger must never indict time it did not witness.

At most **2 candidates** surface (the list supports n), and only rank 0 gets
a primary button — the alternative sits behind an unobtrusive disclosure, so
the common case is one choice, not a form.

Deterministic, no model call, no network — it runs on-device against the
ledger and is unit-tested with synthetic streams
(`test_the_mouse_jiggle_is_not_a_comeback`).

## 6. the offer

One offer per drift episode, created **when the drift is detected** — the
candidates are computable at that moment (the tail is, by definition, the
drift), and creating early means both surfaces share one object. **At most
one unresolved offer per run**: a further drift while one is open extends it
(candidates recomputed over the union of contested time), never stacks.

**Persisted in the sidecar's sqlite** (a small `offers` table: `offer_uuid`,
`run_id`, `created_at`, `returned_at`, `candidates` json, `segments` json,
`status`, `resolution` json, `excised` json) so a sidecar restart
mid-episode keeps the question alive — and **`DriftWatch` hydrates from an
open offer that has not yet returned**, so a restart never re-detects the
same drift into a duplicate episode; an offer whose return was already
recorded must *not* re-arm the episode (the restart's first active sample
would fire a second return and fold legitimate post-return work into the
removal — a recorded return is earlier truth and is never overwritten).

**Extension keeps every gap** (sol's slice-B review): when a further drift
extends the open offer, the previous episode's contested interval
(recommended boundary → its return) moves into the offer's `segments` list
— the ONE user-facing question accumulates each unresolved gap. "Remove"
excises the **union** (segments plus the current episode, at the chosen
boundary); the first gap can never silently become credited just because a
second one happened. A grown span re-emits `rewind.offered`.

**States:** `open → applied | kept`, with `applied → open` again via undo.
There is **no lapse state and no timer that destroys the question** (sol's
review; principle 2). The lifecycle:

1. **the card** — on `return.detected` with an open offer, the deer shows
   the card. The card *is* the welcome (no separate `drift_return` line
   fires when an offer is open).
2. **the chip** — unanswered after ~45s, the card collapses into a quiet
   one-line chip on the deer ("review 17m of quiet time") so the person's
   regained thread is never held hostage by retrospective accounting. The
   chip persists for the life of the run.
3. **resolution at the stopping transition** — pausing, finishing, or
   switching with an open offer *asks as part of stopping*. The deer's
   pause control becomes the combined choice; a bare transition request
   still **stops the clock immediately** (the run freezes, unbanked — see
   below) while the question stays open. The run cannot silently bank
   contested time, and no guard ever keeps a runaway timer running.

**The card, canonically** (impact language; neutral observation; no
inference the person must confirm or deny):

> *ears perk* welcome back. the clock kept running through **about 17
> minutes of quiet**.
>
> **[ remove 17m — clock becomes 14:00 ]**  **[ keep all time ]**
> <small>or: remove 11m (if that blip at 2:20 was you)</small>

At a stopping transition, the same choice wears the transition's clothes:
**[ remove 17m & pause ]** **[ keep all time & pause ]** **[ keep going ]**.

Copy rules, doc-authoritative: quiet is described as *quiet* ("no input for
a while", "quiet at the desk") — never "you wandered", "you left", "you got
distracted". The existing deer caption `"you wandered — that's allowed"`
(`DeerWindow.tsx`) is renamed in slice B to match. "keep all time" carries
zero defensive framing — it is a normal button, not a plea.

**Applying — the excision.** Each applied offer stores its excised
**interval** `[boundary, return_at]` (or `[boundary, now]` for a
rewind-and-pause from afar, so unattended time never credits). Credited
elapsed becomes `wall − union(applied intervals)` everywhere the engine
computes time (`state()`, `_end_run`, `crossed_target`). `started_at` is
**never rewritten** — the original stands as recorded truth. Intervals are
per-offer precisely so undo can lift one cleanly.

**Undo** (sol's review): after applying, the card's resting state is
"17m removed · clock now 14:00 · **undo**". Undo removes the interval,
returns the offer to `open` (the chip comes back — the question is
re-askable, because the tap may have been the mistake), and if restored
credited time sits at or past the target, the run is silently marked
announced — undoing must never fire a celebratory ding.

**The ding, precisely:** the target-crossing announces on **uncontested
credited time** — `wall − union(applied intervals) − open contested span`.
So an unresolved correction can't inflate the clock into a false
celebration, applying a correction that drops credited time under target
re-arms the ding (locked decision), and once the person keeps the time, the
formerly-contested minutes count and the ding fires honestly.

**Transitions while an offer is open:** `POST /v1/focus/pause|finish|start`
accept an optional `resolution` (`{offer_uuid, action: "remove"|"keep",
at?}`) applied atomically before the transition — the deer's combined
buttons always send one, and that's the primary path.

The safety net **freezes, never refuses** (sol's slice-A review — a 409
that leaves the timer running would worsen the exact runaway-timer problem
rewind exists to fix). A bare transition arriving without a resolution
while an offer is open still *acts on time immediately*: the run
**freezes** — `frozen_at` stamps the instant, no further seconds accrue —
but does not bank. The response reports the pending question; the
correction stays open; the chip persists. Resolution then closes and banks
the frozen run with `ended_at = frozen_at` (the answer's own timestamp
never inflates the run). A frozen run is not a running one, so a bare
*start* still begins the new block immediately — the old run simply waits,
frozen and unbanked, for its answer. No path can bank contested time
silently, and no path can keep a clock running against the person's will.

## 7. the wire

**New outbox event types** (server's `KNOWN_EVENT_TYPES` grows by four):

| event | payload | when |
|---|---|---|
| `rewind.offered` | offer_uuid, run task/label, `contested_seconds` | drift detected, offer created (extended offers re-emit with the new span) |
| `rewind.applied` | offer_uuid, removed_seconds, surface | a candidate chosen |
| `rewind.kept` | offer_uuid, surface | "keep all time" |
| `rewind.undone` | offer_uuid, restored_seconds | undo |

**Data minimization (sol's review):** in v1, `rewind.offered` carries the
*length* of the contested span only — **no candidate timestamps**. The
boundaries exist to serve the on-device question; the server needs the
texture (how much time was contested, what was decided), not the forensics.
Candidate details travel upstream only when an **opted-in tether** needs
them to phrase a ping (§8), and only at ping time.

**`session.ended` grows two fields**: `gross_seconds` (wall time) and
`excised_seconds` (the union's total), while `seconds` stays *credited* — so
the entire attribution path (banking, projection, blocks arithmetic) is
untouched and honest by construction, and the second truth rides alongside.

**Pip stays quiet.** No per-correction observation (sol's review: a
check-in, a welcome, a card, a confirmation, *and* a pip noticing turns a
gentle feature into a conspicuous one). The gross-vs-credited texture rides
`session.ended` into the data edwin eventually reads; if pip ever remarks on
rewind patterns it's at retrospective cadence, never per-event.

## 8. the tether fallback (second surface)

The desktop card only fires on *return* — if you never come back to the
desk, no card ever shows and the block runs unattended. That's exactly the
case a phone ping covers.

- **opt-in, separately** (sol's review): the tether surface is its own
  consent — linking telegram does not by itself export candidate data
- server-side, a watcher sees `rewind.offered`: if the offer is still
  unresolved after the away threshold (**25 min**) and the user opted the
  tether in, it requests the candidate impact-framings from the device path
  and sends one ping with inline choices: *"your block's still running but
  it's been quiet a while — remove 25m and pause, or keep it all?"*
- the answer lands server-side as a **pending decision**
  `{offer_uuid, choice, decided_at, source}`
- **the return channel piggybacks on the pump**: while (and only while) the
  sidecar has an open offer, its 15s sync loop also asks
  `GET /api/v1/sync/decisions`; an answer is applied through the same code
  path as a card tap — for the away case, as
  rewind-and-pause / keep-and-pause
- **first answer wins, the sidecar is authoritative.** If the run already
  ended when a decision arrives, the decision is refused as stale (live
  runs only) and the offer's terminal state stands. The eventual
  `rewind.applied`/`rewind.kept` event closes the loop server-side.

Staging: v1 ships the desktop card/chip only — but the offer object, event
types, and impact-framed candidates are the full design, so the tether
surface bolts on (with phase 7's tether work, or earlier via the existing
bots) without touching the sidecar's core.

## 9. platforms

Everything above the collector is pure python on `(t, idle)` pairs —
platform-agnostic by construction. The platform surface is exactly one
function:

| signal | macos (shipped) | windows (v1 requirement) |
|---|---|---|
| idle seconds | `CGEventSourceSecondsSinceLastEventType` — no permissions | `GetLastInputInfo` (user32) — no permissions |
| sleep/lock | free via idle (counter climbs through sleep) | same |
| frontmost app | `NSWorkspace` (shipped, blocklist only) | later, with the app-context phase |

The windows webview origin (`http://tauri.localhost`) is already in the
sidecar's CORS allowlist; no other windows work is implied.

## 10. what rewind will not do

- **auto-rewind, ever** — the output of the algorithm is a question, never
  an action
- **destroy a pending correction** — not on pause, not on finish, not on
  switch, not by timeout; the question defers (the chip) until the person
  resolves it
- **make the person defend themselves** — buttons state impact on the
  clock; no "were you working?" framing anywhere, including ambient
  captions
- **stack questions** — one unresolved correction per run; new drifts
  extend it
- **name apps in v1 offers** — idle-only; candidates say *how much*, not
  *what you were doing*
- **touch a banked run** — the excision happens on live clocks only
- **send raw activity upstream** — moments and candidate timestamps stay in
  device memory; contested length and decisions are the only v1 export
- **score the drift** — gross-vs-credited is texture for kindness, not a
  metric; nothing about rewind ever renders as a streak, phase, or grade,
  and pip never comments per-correction

## 11. the numbers (authoritative — change this doc first)

| number | value | meaning |
|---|---|---|
| drift threshold | 10 min (existing `DRIFT_IDLE_MINUTES`) | idle that opens an episode *and* mints the offer |
| `gap_break_s` | 120 s | a gap that splits activity runs |
| micro-run floor | < 3 moments or < 60 s | run mass below which an edge is a rank-1 disclosure candidate |
| candidates surfaced | ≤ 2 | struct supports n; only rank 0 gets a primary button |
| card → chip | ~45 s | unanswered card collapses to the quiet chip (never expires) |
| coverage slack | 30 s | how late the ledger's witness may begin and still count as covering the run's start |
| away threshold | 25 min | unresolved offer earns one opted-in tether ping |
| decision poll | 15 s | the pump's existing cadence, only while an offer is open |

## 12. building it

| slice | contents | notes |
|---|---|---|
| **A · the ledger & the question** | `ActivityLedger` (input-moment reconstruction), `rewind_candidates()` pure fn, synthetic-stream tests | no behavior change; pure + testable |
| **B · the offer & the card** | offers table + `DriftWatch` integration + hydration-on-boot, excision intervals + credited/uncontested-time arithmetic + ding logic, undo, `resolution` on the focus transitions + 409 safety net, deer card/chip UI + neutral-copy pass (incl. the `"you wandered"` caption), 4 event types server-side, `session.ended` gross/excised | **the v1 ship line** |
| **C · the second door** | tether opt-in, server offer watcher + ping, pending decisions + `GET /api/v1/sync/decisions`, stale-decision refusal | rides phase 7's tether (or the existing bots sooner) |

**The test list** (names first, the usual way):

- `test_the_mouse_jiggle_is_not_a_comeback` — a lone moment mid-gap ranks
  as a disclosure candidate, never the recommendation
- `test_thinking_pauses_dont_split_the_run` — sub-`gap_break` quiet stays
  inside one run
- `test_pause_never_banks_contested_time` — bare pause with an open offer
  freezes the run unbanked; pause-with-resolution banks the chosen truth
- `test_a_bare_pause_still_stops_the_clock` — the safety net freezes,
  never refuses; no guard keeps a runaway timer running
- `test_a_frozen_run_banks_at_its_freeze_instant` — a late answer never
  inflates the run past `frozen_at`
- `test_switch_is_a_stopping_transition_too` — starting another task rides
  the same rule (old run freezes, new block starts immediately)
- `test_a_restarted_ledger_never_recommends_the_session_start` ✓ slice A —
  the witness rule; observed edges or nothing
- `test_the_idle_counter_carries_coverage_across_restarts` ✓ slice A — a
  restart during pure quiet loses no truth
- `test_impact_is_computed_at_resolution_time` ✓ slice A — boundaries
  persist, amounts derive at ask/apply
- `test_future_moments_are_not_evidence` ✓ slice A — nothing past `now`
  places a boundary
- `test_the_chip_outlives_the_card` — collapse is deferral, not expiry
- `test_the_ding_rearms` — a correction under target → target announces
  again
- `test_contested_time_never_dings` — the target waits on uncontested
  credited time
- `test_undo_restores_the_minutes_and_the_question` — interval lifted,
  offer re-opens, no celebratory ding on the way back up
- `test_a_second_drift_joins_the_first_unresolved_gap` ✓ slice B — one
  question per run; segments accumulate; remove excises the union
- `test_undo_restores_every_gap_of_the_union` ✓ slice B
- `test_a_held_blocks_question_cannot_ride_another_runs_transition`
  ✓ slice B — ownership + the next-offer handoff
- `test_hydration_respects_a_recorded_return` ✓ slice B
- `test_restart_hydrates_the_open_offer` — no duplicate episode, question
  survives
- `test_started_at_is_never_rewritten` — excision intervals only; gross
  stays true
- `test_two_truths_ride_session_ended` — credited banks, gross travels
- `test_candidate_timestamps_stay_home` — `rewind.offered` carries length,
  not forensics
- `test_a_banked_run_is_beyond_reach` — stale decisions refused
- `test_first_answer_wins` — card tap vs tether answer race
- `test_sleep_reads_as_one_long_gap` — lid-close → boundary at the lock
- `test_excisions_accumulate_across_episodes` — resolved drift, later
  drift, one run, disjoint intervals

## north star

The person who forgot to pause — which is the person this whole product is
for — comes back from twenty lost minutes to a deer holding one gentle
question instead of a lying progress bar. The question waits without
nagging, survives the pause button, states its options as plain arithmetic,
and takes "keep all time" for a full answer. One tap either way, the truth
is restored, nobody was graded, nobody had to explain themselves, and the
comeback got welcomed. The clock keeps faith with reality, so everything
downstream of the clock can too.
