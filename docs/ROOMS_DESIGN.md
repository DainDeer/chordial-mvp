# chordial: rooms, cycles, and the council

**status:** design doc v2 · direction locked
**date:** 2026-08-11
**stack:** tauri shell · react + typescript frontend · bundled python sidecar · chordial server (postgres)
**supersedes:** the v1 rooms concept doc and the velvet antler design doc
**pretty version:** published as a claude artifact (visuals + mockups); this file is the authoritative repo copy

---

## 1. what this is

Chordial is evolving from a chat companion into a **conversation-native personal
operating environment**. Instead of one endless thread, the user moves through
small, purpose-built **rooms** — a room for today, a cycle-planning room, a
project work room, a temporary "help me get unstuck" room, or simply a room to
talk.

The space is inhabited by the **council**: small animal characters with
names, personalities, lanes, and speaking policies — chaired by a deer who also
lives quietly on the desktop as an ambient body-double. Beneath every room sits
one shared reality: cycles, plans, tasks, events, observations, and decisions in
canonical storage. Conversations produce structured consequences; old rooms stay
browsable but new reasoning never replays old transcripts.

It should feel less like "a chatbot with lots of agents" and more like **a cozy
personal workspace inhabited by a tiny collaborative team** — one that works
hardest when you need it, and fades to a warm, watchful quiet as you find your
feet.

### decisions locked 2026-08-11 (don't re-litigate)

1. **Authored cast.** The council ships with defined characters. Every name,
   species, and look is user-reshapeable, but nobody starts as a blank form. A
   new creative/hobby helper takes over the lanes of the retired
   music/writing/comfort trio (aria, poet, mochi — comfort folds into Mabel).
2. **Velvet antler is folded in completely.** The desktop deer, pomodoro
   sessions, and ambient presence are core chordial — not a sibling app. The
   deer chairs the council.
3. **The arc bends, it doesn't end.** As the user becomes their more productive
   self, chordial gets less intrusive — but stays as a barrier against sliding
   back. Sentinel mode, not goodbye.
4. **The tether** — a single-bot Telegram bridge into today's room (§9). The
   per-helper multi-bot group chat is retired outright: delete, don't port.
5. **"Cycles," not "sprints."** Existing vocabulary stays; conversation-flavored
   engine vocabulary is renamed where it conflicts (§11).
6. **Client/server from day one, designed for 100+ users at every stage.**
   Server holds canonical state and all LLM calls; the desktop app is a rich
   local client with its own sqlite and an offline ambient engine (§10).

---

## 2. principles

- **Chat is a medium, not the whole app.** Home is orientation, not a blank
  message box. Structured state stays visible outside conversation.
- **Rooms are the interaction boundary.** A room = purpose + participants +
  scoped context + conversation + artifacts + lifecycle. Its type decides who
  attends, what context loads, and what outputs are expected.
- **One shared reality.** Helpers don't own competing memories. Rooms and
  helpers are views and reasoning processes over canonical state.
- **Helpers reason more than they speak.** Receiving an event, reasoning about
  it, emitting structured output, and speaking are four different privileges.
  Default: one speaker per turn.
- **Structured consequences outlive conversations.** A room's durable output is
  events, observations, decisions, and a summary — not its transcript.
- **The user never organizes the bureaucracy.** "i want to work on chordial for
  a while" is enough. The system arranges itself around the user, not the
  reverse.
- **Success deletes bureaucracy.** Reliable routines lose their nudges; tracking
  that stops informing gets retired; a thriving user gets a quieter council.
- **Silence is the default gift.** (from velvet antler) Presence without demand.
  Every proactive word costs attention budget; the strongest words are scarce on
  purpose.

---

## 3. the council

Seven residents. One character definition drives both AI runtime behavior and
UI presentation. Personalities shape presentation; underlying observations stay
structured and evidence-tied.

| helper | species | lane | character direction |
|---|---|---|---|
| **vel** (chair) | 🦌 deer | orchestrator & ambient presence | holds the room; routes every event, budgets the council's attention, lives on the desktop as the body-double; speaks for the house |
| **pip** | 🐿️ squirrel | productivity | cycles, focus blocks, task breakdown; bright, practical, eager — turns intimidating work into tiny pieces |
| **skip** | 🐰 bunny | movement | cheerful and energetic without being gym-bro-y; five minutes counts |
| **remy** | 🦝 raccoon | nutrition | casual, resourceful, happy with approximate estimates |
| **mabel** | 🐻 bear | wellbeing | warm protective mama-bear energy; rest, sustainability, the whole organism (absorbs mochi's comfort lane) |
| **juniper** (new) | 🦊 fox | creative & hobbies | keeper of sparks — art, music, writing, play; curious, a little mischievous; defends unproductive joy as a first-class goal |
| **edwin** | 🦉 owl | scoring & reflection | kind old librarian; precise, evidence-driven, gently skeptical; speaks rarely |

Alternates considered for the creative helper: magpie (collector of shiny
things), otter (playful tool-user). "Vel" (from velvet) is a working name.

### identity policy

Characters are **authored defaults, personally reshapeable**. The definition
ships in YAML (identity, personality, visual set, capabilities, speaking
policy); every user-facing aspect — name, species, pronouns, vibe — can be
renamed or reshaped in conversation and stored as a per-user override in the
database. The old representation ritual survives as an optional "make them
yours" moment rather than mandatory onboarding.

### example character definition

```yaml
id: productivity

identity:
  name: pip
  species: squirrel
  role: productivity companion

personality:
  traits: [energetic, practical, encouraging]
  speaking_style: concise
  disagreement_style: gentle_but_direct

visual:
  avatar: pip.png
  expression_set: [neutral, excited, thinking, concerned, celebrating]
  accessory: tiny_clipboard

capabilities: [cycle_tracking, focus_blocks, task_breakdown]

room_behavior:
  default_rooms: [daily, cycle, work_session]
  speak_when:
    - productivity_relevant
    - directly_addressed
    - important_pattern_detected
  otherwise: listen
```

### speaking policy

Every message routes through three layers, cheapest first (the velvet antler
scheduler conclusion, imported verbatim):

```text
message ──► routing rules ──► decider ──► reasoning ──► ONE speaker
            deterministic     small fast   1–3 helpers
            room type ×       model, only  emit silent
            event type;       on genuine   observations
            no tokens spent   ambiguity;
                              may choose
                              silence
```

- guarantees live in arithmetic and rules — never a model
- a cheap enum-constrained decider handles gray zones only
- expensive reasoning is the last resort, and reasoning ≠ speaking
- **default: one speaker per turn**; council debate (productive disagreement,
  multiple speakers) is an explicit mode of planning/review rooms, never
  ambient behavior

---

## 4. rooms

Room types are permanent categories or temporary meetings:

```text
workspace
├── 🌤 daily rooms          one per day; the default place to be
├── 🏃 cycle rooms          planning · mid-cycle review · retrospective
├── 🛠 project rooms        work sessions hydrated with project context
└── ✨ ad-hoc rooms         get unstuck · make a decision · brain dump · just talk
```

Room creation begins with intent, not configuration — the chosen intent
determines template, participants, context, and available UI. The user is
never asked to pick agents or attach context.

### daily room lifecycle

Rooms move through an explicit, idempotent state machine:
`open → closing → closed`. Every transition is safe to retrigger; the close
pass is safe to re-run (exactly one summary per room, enforced by a unique
constraint on `room_summaries.room_id`).

- **Lazy open, lazy close.** Created on first interaction of the day; the
  `open → closing` transition fires on the first interaction of the *next* day
  or after long idleness — never a midnight cron. A 1am "i can't sleep" lands
  in Monday's room, where it belongs.
- **Close pass.** A silent, curator-shaped pass distills the conversation into
  durable outputs: events, observations, decisions, task updates, and a daily
  summary. Runs asynchronously (pulse rhythm, `delivery: none`) and marks the
  room `closed` when its outputs are committed.
- **The new room never blocks on the close pass.** The first turn of a new
  daily room must not race yesterday's summary. Its briefing hydrates from
  (a) canonical structured state, which is *always* current — events, task
  updates, and observations are written throughout the day, not at close — and
  (b) yesterday's summary **if** the close pass has committed it. If the
  summary isn't ready yet, the briefing instead includes a compact structured
  digest (yesterday's events + open threads) marked `summary: pending`, and the
  close pass backfills. No turn ever waits on an LLM summarization job.
- **The next day's room reads only the compressed consequences** — the raw
  Tuesday transcript is never in Wednesday's prompt. (This is also the prompt-
  cache win: a closed room's prefix never changes again.)
- **Archives are forever.** Past rooms form a browsable journal-like timeline.
  Reopening one is read-only with a gentle "continue in today's room?"
  Room logs are never silently deleted: the existing per-user event-log
  trim/cleanup path is **retired in phase 2** (it contradicts browsable
  archives). If storage ever demands it, compaction to cold storage or a
  user-visible retention policy are the acceptable shapes — silent deletion is
  not.

### the cycle rooms, as built (phase 6b)

- **Two undated room types anchor to the sealed cycle.** `cycle_retro`
  and `cycle_planning` carry a subject anchor (`subject_type='cycle'`,
  `subject_id='c12'` — the assessments vocabulary) instead of a date;
  a unique index on (user, room_type, subject_id) is their exactly-once
  guard (null dates made the dated constraint vacuous for them).
  Planning anchors to the cycle *just lived* on purpose: the
  conversation shapes the next cycle, but its evidence base — the retro
  summary, the filed scorecard — belongs to the last one.
- **Opening a cycle room closes the user's other open cycle rooms** —
  and walking back into a settled one REOPENS it. One cycle
  conversation at a time; the pair is one conversation revisited, never
  a locked door with a composer painted on. The close compresses a
  retro into the summary the planning room hydrates, and a reopened
  room's new exchanges recompress into that summary on the next close
  (`close_room` refreshes an existing summary with the fresh digest).
  The settle runs AFTER the open commits, so two devices opening
  different doors concurrently converge to at most one open room; in
  the pathological same-instant case both may settle each other closed
  (never two open), self-healing on the next walk-in. Both send
  boundaries require status `open` — a room mid-`closing` refuses
  turns, because its digest is already compressing; a turn that raced
  the close anyway is folded in by the stale-summary sweep, which
  `open_cycle_room` runs before planning hydration reads anything.
  Daily rooms are untouched; a cycle room nobody follows up on simply
  stays open.
- **The retro is a subpoena.** Opening it scores the cycle on demand
  when no card is filed, waiving only the grace watermark — the person
  showing up for the review has called the evidence question — never
  the completeness requirement (no seal, no retro). The card records
  which way it sealed (`detail.cycle.scored_by: sweep | retro`).
- **Edwin presents the filed card, exactly once per card, repaired on
  every open**: an authored frame around a deterministic render of the
  assessment he already wrote at scoring time. No new model call, no
  invented number — the deer's authored-lines discipline applied to the
  owl. A metadata marker on the seeded event is the durable
  presented/not-presented state: a retro that first opened cardless
  (scoring failed, no scorer wired) gets an honest "not written yet"
  note, and a later open presents the eventual card ("now written — as
  promised"); a crash between room creation and seeding repairs the
  same way. The briefing matches the transcript in both states — a
  cardless room is briefed as cardless, never as presented.
- **Hydration is family-shaped.** `latest_summary` filters to
  daily/legacy so a retro closing last never becomes "previously:" in
  tomorrow's daily room; planning hydrates *its* cycle's retro summary
  (by subject, never whichever retro closed last) plus the card render;
  the retro's ambient only orients — the presentation is already in the
  transcript.
- **The director folds both types into the `cycle` family** (as
  legacy folds into daily): vel, pip, mabel, and edwin sit down for
  cycle rooms; the decider handles the rest, unchanged.
- **Room-bound turns.** `POST /api/v1/rooms/{uuid}/messages` runs a
  turn into an open owned room under the same receipt contract as the
  daily send; closed rooms answer 409 (remembering is the GET's job),
  foreign rooms 404 exactly like never-existed. Delivery payloads carry
  the room, so one room's lines never render inside another room's
  view on the shared per-user socket. Doors:
  `GET /api/v1/rooms/cycle`, `POST /api/v1/rooms/cycle/{retro|planning}`.

### room artifacts

Conversation can produce durable interactive objects backed by canonical state,
rendered by React in the app and degraded to text + inline buttons on the
tether: next-action cards, cycle proposals, decisions. v1 ships `next_action`
only.

---

## 5. presence & focus (velvet antler, folded in)

The deer lives on the desktop — a small always-there window that is **presence
without demand**. The body-double: someone in the room while you work, whose
default state is companionable silence.

- **Sessions are owned locally.** The pomodoro clock, drift detection, and
  ambient reactions run in the bundled sidecar against the local database —
  they work offline and never lag on a network call. A block completed on a
  plane syncs when you land.
- **Starting is one tap** from a next-action card — in the daily room, on a
  project page, or on the deer itself. A block is 25 minutes of effort, not a
  promise of completion.
- **Drift and return are handled gently.** Rust-side collectors watch only the
  frontmost app's bundle id and idle time (never window titles, never content).
  Ten minutes adrift earns at most one soft check-in; an unanswered check-in is
  silently assumed to be a break, never re-asked.
- **The potency budget survives the merge.** Ambient words are metered (points
  per hour, cooldowns per theme, week-scale cooldowns on the strongest
  affirmations); the never-say list runs as an output validator; per-theme
  instant mute is a right.
- **Session outcomes become events** — `focus_block.completed` flows into the
  shared reality, Pip processes it, cycle progress moves, Edwin eventually gets
  honest data.

### what the deer will not do

- read window titles, screens, or content — bundle ids and idle time only,
  sanitized at the Rust boundary before anything reaches the engine
- carry coach pressure — no streaks, phases, or assessments ever render in
  ambient presence
- guilt-trip a drift — one soft check-in, then trust
- spend affirmations cheaply — the strong words keep their scarcity
- speak during meetings — blocklisted apps auto-silence everything

---

## 6. cycles

Planning runs on **cycles** — two-week rhythms with a theme, a capacity
estimate in focus blocks, one or two focus projects, explicit commitments,
guardrails, and intentionally unallocated slack. The cycle is structured; the
day stays adaptive, with exactly one clear next action per active commitment to
minimize activation energy.

```yaml
cycle:
  theme: build rhythm without overloading myself
  span: aug 10 – aug 23            # two weeks
  capacity: 24 focus blocks        # 25 min each
  focus_projects: [chordial, health_routine]

  commitments:
    - room runtime v1 · chordial · 5 blocks · high
      next_action: define the room model (1 block)
    - 4 resistance sessions · health · medium

  guardrails:
    max_planned_blocks_per_day: 6
    unallocated_capacity: 20%
```

Cycle planning is a room-based workflow: orient (previous cycle, goals, honest
historical capacity) → intent in natural language → council proposal →
conversational negotiation → **freeze an immutable baseline**. Later changes
are welcome but become explicit `scope_change` events instead of silently
rewriting history — honest data for Edwin, guilt-free changes of mind for the
user.

### baseline persistence model

"Immutable baseline" has a concrete shape (tasks and cycles today are mutable
rows, so this must be explicit):

- **Commitments are first-class rows** with stable UUIDs (`commitment_id`),
  belonging to a cycle and optionally referencing a plan and/or task. Focus
  events reference `commitment_id` — never a free-form slug — so progress
  attribution survives renames.
- **Freezing writes a baseline snapshot**: an append-only record of the
  accepted commitments and allocations at freeze time (`cycle_baselines`, one
  per cycle, unique-constrained). The live commitment rows may evolve; the
  snapshot never does.
- **Scope changes append.** `scope_change` rows (reason, deltas, timestamp)
  are the only sanctioned way planned capacity moves after freeze.
- **The current view is a projection**: baseline + scope changes + progress
  events. Edwin compares projection against baseline; that diff *is* the
  planning-accuracy signal, and it stays reliable because both sides are
  append-only.

> Cycles, plans, goals, tasks, wins, and check-ins already exist in the
> workspace schema with locked lifecycle conventions (completed vs. released,
> no hard deletes, `closed_at` stamping — see
> [NATIVE_WORKSPACE_DESIGN.md](NATIVE_WORKSPACE_DESIGN.md) §2.0). This section
> *extends* `Cycle` with theme, block capacity, commitments, and scope-change
> events — it does not replace it.

---

## 7. the arc: fade, don't leave

Chordial works itself out of the loud part of the job. The council's attention
budget is highest when support is needed most, then deliberately tapers as
consistency becomes the user's own — but it never tapers to zero. The end state
is a **sentinel**: a quiet companion who still holds the door against old
habits.

| phase | council posture | what changes |
|---|---|---|
| **settling in** | curious, frequent, calibrating | baseline observation; the intensity contract is agreed here — explicit consent for how pushy is welcome |
| **building** | full coaching | daily rooms, cycle discipline, active nudges, the deer at your side |
| **rhythm** | tapering | reliable routines lose their nudges one by one (inverted-backoff arithmetic: success extends the quiet); tracking that stops informing is retired on Edwin's recommendation |
| **keeping watch** | sentinel | presence stays (the deer, the daily room, the tether); proactive voice reserved for evidence-backed early-warning patterns — skipped cycles, vanishing movement, joy-starvation — surfaced gently, re-escalation only by agreement |

This is the covenant answer to "does it create dependency?": the product's
success metric is how quiet it gets to be — while remaining the barrier between
the user and the slide back.

### the taper, as built (phase 6c)

The arc's arithmetic spine, in `src/services/taper.py`. The scorecard ledger
(§8) is the evidence base; the taper reads the recent cycle cards — ordered by
the cycle's **seal**, never by when a backfilled card happened to be filed —
and answers one question: how much quiet has this person earned?

- **The steady test.** A cycle is steady when its card's consistency and
  execution both clear their floors (`TAPER_STEADY_CONSISTENCY`,
  `TAPER_STEADY_EXECUTION`, default 0.6 — v1 guesses, flagged tunable) and
  sustainability, when computable, isn't poor (`TAPER_MIN_SUSTAINABILITY`,
  0.5): unsustainable bursts never earn quiet, even with perfect numbers.
- **The inverted backoff.** Each consecutive steady cycle (newest-first)
  doubles the check-in interval; the streak breaks at the first cycle that
  isn't steady. A wobbly cycle resets to full coaching. An **unjudged** cycle
  (scores that couldn't be computed — nothing planned, never frozen) earns
  nothing either: absence of evidence never extends the quiet. The taper
  reads the **whole** cycle ledger (one row per cycle by the exactly-once
  constraint) and orders by seal *before* the window truncates — a
  filing-ordered LIMIT would let backfilled old cards displace the latest
  evidence entirely.
- **Never to zero.** The multiplier caps (`TAPER_MAX_MULTIPLIER`, 8×) — the
  sentinel keeps its post. The postures map onto the arithmetic: no cards yet
  = *settling in*, streak zero = *building*, earned quiet = *rhythm*, at the
  cap = *keeping watch*.
- **Applied at the pulse.** `ChordialPulseSource` computes the beat per user
  each cycle (guarded: a taper failure is the flat base beat, never a stalled
  check-in). A stretched beat **versions the rhythm key** (`checkin@480`):
  the pulse store persists a next-check horizon per key and consults it
  before the interval is re-evaluated, so a reset under the same key would
  sleep out the old stretched horizon — a changed beat lands on a fresh key
  whose due recomputes from the message anchor immediately, and the base
  beat keeps the bare `checkin` id so untapered state is untouched. It
  composes with the backoff gate, not replaces it: the taper sets the beat
  from earned evidence, the backoff still stretches ignored chains on top.
  First contact stays due-now; quiet hours still hold.
- **Visible, honestly.** `GET /api/v1/arc` reads the same numbers the pulse
  uses (posture, streak, per-cycle verdicts with reasons); the app states it
  as one quiet line. The council's ambient briefing gains a posture line
  **only once quiet is earned** — an untapered user's prompt bytes are exactly
  the pre-6c bytes.

Deferred, not faked: per-routine nudges lost "one by one" (exactly one
proactive rhythm exists today — the check-in beat — so the taper stretches
that one) and Edwin's retire-this-tracking recommendations; both arrive when
there are more nudges to lose.

---

## 8. edwin & the scorecard

Edwin is deliberately separate from Vel. Vel asks *"given what we intended and
what's happening, what should we do next?"* Edwin asks *"given what we intended
and what actually happened, what are we learning about the plan?"*

**He scores the system, never the person.** No single productivity grade —
instead: execution, planning accuracy, priority alignment, consistency,
sustainability, each with evidence attached.

He hunts patterns: tasks that roll cycle to cycle, quietly growing scope,
systematically optimistic estimates, exciting code eating the creative goals,
unsustainable bursts, nudges that don't change behavior. And — just as
important — **simplifications**: routines that no longer need nudges, tracking
that stopped informing, goals consistently exceeded, tasks easier than
expected. In sentinel phase his pattern library becomes the early-warning
system.

He speaks at cycle planning, mid-cycle review, retrospectives, and on strong
evidence-backed patterns. Otherwise he reads, and files findings.

```yaml
cycle_health:
  execution:      { score: 0.78, evidence: [11 / 14 expected blocks completed] }
  prioritization: { score: 0.91, evidence: [8 / 9 work blocks on high-priority objectives] }
  estimation:     { score: 0.62, findings: [event pipeline underestimated ~40%] }
  consistency:    { score: 0.71 }
  sustainability: { score: 0.84 }
```

### the scorer, as built (phase 6a)

The scorer splits judgment from arithmetic, hard — the cadenza lesson
("the scorer proposes, the transcript disposes") applied to cycles:

- **Every score is computed, never generated.** `scorecard.py` derives all
  five components from the projection (baseline + scope changes + progress)
  and the session ledger: execution = landed share of the plan (overshoot
  clamps at 1.0 — estimation judges the gap, execution never punishes it
  twice); prioritization = weighted share of *attributed* time against the
  priorities **as frozen** in the baseline (a priority edited after the
  freeze can't recast earlier blocks; time on commitments with no frozen
  priority — unprioritized at the freeze, or added afterward — is neither
  credit nor blame); estimation = mean per-commitment accuracy against the
  **frozen** baseline (released commitments stay in — releasing moved
  scope honestly, but the estimate was still an estimate); consistency =
  active days over elapsed days (the denominator stops at the **seal
  date**, so an early-closed cycle — or one scored after downtime — is
  judged on the days it lived); sustainability = the daylight share of
  focus time (late-night window 23:00–06:00). Sessions are judged as
  user-local **intervals** `[ended_at − seconds, ended_at]`, split at
  midnight for day counting, split at the late-window edges, and clipped
  to the cycle window — a block ending at 06:00 ran through the late
  hours, and a run straddling the boundary contributes exactly its inside
  part. Events without a device wall clock fall back to their apply stamp
  (same coalesce as the projection). A component that can't be honestly
  computed is `score: null` with the reason in its evidence. There is
  deliberately no overall grade.
- **The model writes only prose.** The shared utility model (curator /
  reconciler / decider pattern, `role="scorer"`, billed to edwin) produces
  a short summary and at most six findings; every finding must cite
  evidence refs (`cm:<uuid>`, `sc:<id>`, `ob:<id>`, `component:<name>`)
  that the validator resolves against the rows actually gathered — and
  the observation pool itself admits only rows whose **user-local date**
  falls inside the cycle, so an adjacent cycle's noticing never becomes
  citable. A claim pointing at nothing is structurally unrecordable —
  dropped and counted. A dead, absent, or garbling model degrades to a
  deterministic summary; the scorecard files either way.
- **The close is the seal — exactly once, and the ledger starts now.**
  Only cycles the person **closed** (status complete) are scored, and only
  after `CYCLE_SCORER_GRACE_HOURS` (default 24) have passed since the
  close — the grace is the evidence watermark, giving an offline device's
  outbox its window to drain before the card seals. An active cycle,
  however overdue, is never scored: it can still be extended and still be
  worked. One assessment per (user, `cycle`, public id), enforced by a
  unique index + the focus_flow insert discipline; the gather pass
  re-checks the seal, so a cycle reopened between discovery and filing
  gets no card. Accepted residuals, stated rather than hidden: a device
  offline longer than the grace loses representation in the card, and a
  cycle reopened *after* its card filed keeps the card it earned as
  closed. Discovery (a supervised watcher beside the pulse,
  `CYCLE_SCORER_SWEEP_SECONDS`) looks back `CYCLE_SCORER_BACKFILL_DAYS`;
  pre-phase-6 history is left unscored rather than judged on evidence
  never collected.
- **Reading is chat, writing is not.** `list_assessments` (edwin's card,
  plus the chair via the full registry) and `GET /api/v1/scorecards` are
  read-only; the scorer is the sole writer. Edwin *presenting* a scorecard
  happens in the retro room (§4, "the cycle rooms, as built") — a
  deterministic render of the filed card, never a fresh generation. The
  grace period has exactly one waiver: the retro door (the person calling
  the question), recorded on the card as `scored_by`.

---

## 9. the tether

Rooms live on the desktop, but ADHD support fails if it only exists on the
machine you're avoiding. The tether is a **Telegram window into today's daily
room** — the same room, the same stream, from the phone.

- **One bot, whole council.** A single chordial bot per platform; speakers are
  attributed inline — `🦝 remy — burrito logged, ~650`. This replaces the old
  one-bot-per-helper group chat, which is **retired outright** (unscalable past
  a handful of users; the rooms model doesn't need it). The tether is
  deliberately a bridge — the long-term phone story is a native companion app
  (deferred until the model proves out).
- **One room only.** The tether always maps to the current daily room — never
  project or planning rooms. The phone is for life logistics and staying held;
  deep work belongs at the desk. No room bureaucracy over chat.
- **Same stream, marked provenance.** Telegram messages append to the daily
  room with platform provenance (the event model already carries this); the
  desktop shows them seamlessly. One conversation, two windows.
- **Presence-aware delivery.** Every proactive word picks exactly one channel:
  desktop active → the deer's bubble; away or idle → Telegram. Never both, and
  channel choice never increases the total — the attention budget is spent
  *before* routing.
- **Quick logging works anywhere.** "ate lunch" from the bus is Remy's event
  either way. Artifacts degrade gracefully: a next-action card becomes a
  message with inline buttons ("start when you're back at your desk?" → queued,
  and the deer offers it on return).
- **Server-side by construction.** The bridge runs on the chordial server (the
  single-bot plumbing and single-use link-code flow carry over; the multi-bot
  ensemble code does not); the desktop app being asleep never breaks the phone.

### the tether, as built (phase 7a)

- **The collapse.** One `TelegramInterface` per deployment (`TELEGRAM_TOKEN`
  + `TELEGRAM_BOT_USERNAME`), registered with the router as
  `(telegram, None)` — the single-bot key, so it serves ANY speaker. The
  ensemble went whole: per-helper tokens/usernames, the shared crew group and
  its update deduper, @-mention parsing, `/setup_group`, the meet-the-guides
  deep links, and `begin_introduction` were deleted, not ported. With the
  multi-human group gone, every chat scope is a private room with exactly
  one human — which also retired the dm-only guards on the code-minting
  tools, so "connect my telegram" works from the app's council room (where
  the ask actually happens).
- **Guides are met in the room, and `meet_guide` makes that real.** The
  director casts only ACTIVE helpers, so deleting the meet links needed a
  replacement transition (Sol's 7a round): when the person says yes to
  meeting a guide, the acting helper calls `meet_guide` — the guide moves
  to active (declined/disabled are re-meetable by design; the tool only
  runs because the user just asked) and can be given the floor from the
  next turn. Every card that can offer the roster carries the tool, and
  the cards' council prose now says so.
- **Inbound is a room turn.** A known sender's message is a group-scope
  stimulus into today's daily room (in private chats `chat_id == user_id`,
  so the group delivery target is the sender); the routing spine picks who
  answers, exactly as it does for the app. The front door now runs on every
  surface — a brand-new person's first tether message lands as the chair's
  dm-shaped introduction, so identity talk stays out of shared summaries.
  Stranger policy is unchanged: no user row, no model call, one static line.
- **Attribution is rendered, not configured.** `attributed(speaker, text)`
  prefixes `{emoji} {id} — ` from the persona card at send time (deterministic
  and total: unknown/absent speakers pass through). Chunking happens after
  attribution, so the prefix counts toward telegram's 4096 cap. (Writing
  that test found a latent `chunk_message` bug: unbroken text one char past
  the cap spun the hard-split loop forever — fixed and pinned.)
- **Presence is the app interface's trichotomy.** The desktop shell owns ONE
  server websocket (it outlives navigation, so proactive lines confirm even
  from the home screen) and heartbeats `{type: "presence", idle_seconds}`
  every 30s from the sidecar's OS idle clock. The server keeps a
  per-user, **per-connection** registry on the `AppInterface`, aggregated
  any-active-wins (Sol's 7a round: an idle laptop's heartbeat must never
  overwrite an active desktop's — each report lives and dies with its
  socket): **absent** (no connected surface), **idle** (surfaces connected
  but none demonstrably attended — every reporter idle past
  `PRESENCE_IDLE_SECONDS` or gone quiet), **active** (any fresh non-idle
  heartbeat; a user whose surfaces never report — a pre-7a client — fails
  open to active). The pulse's
  stimulus factory routes each firing on it: active → the desk; idle → the
  phone by recency, falling back to the still-connected desk rather than
  going silent; absent → messaging platforms only (a closed app can never
  confirm a send). One target per firing, never both; a broken presence
  source degrades to absent — it costs the desk preference, never the
  check-in. Without a web surface the targeting is byte-for-byte pre-7a.
- **One conversation, two windows.** A council line CONFIRMED delivered to
  telegram is mirrored best-effort to the user's connected app surfaces by
  the router; the sender's own phone line is mirrored by the interface
  before the council answers. Mirrors never touch the delivery verdict —
  the event log records against the telegram confirmation, and the desk's
  next history fetch covers anything the echo missed. Mirrored payloads
  carry `author_type` + `platform`; the desktop renders the person's phone
  lines on their side of the room with a provenance whisper ("📱 from
  telegram"), and daily-room council lines that land while today's room
  isn't the watched view become an unread nudge on its door — they wait,
  they don't chase. The nudge is room-aware (Sol's 7a round): it counts
  daily lines even while a cycle room is open (they're filtered from that
  view's transcript, so they were never seen), never counts a cycle room's
  lines against the daily door, and re-resolves an unrecognized room id
  once (midnight mints a new daily room) before deciding. Only entering
  today's room clears it.
- **Deferred, not faked:** artifact degradation to inline-button messages
  ("start when you're back at your desk?" → queued for the deer) — the
  rewind ping is still the only interactive tether surface; the deer
  voicing server check-ins in its own bubble (they land in the daily room
  with the door nudge for now).

---

## 10. architecture for 100+ users

Client/server from day one — even while there's exactly one user. The server is
the brain and the source of truth; the desktop app is a rich local client that
owns everything latency-critical, privacy-critical, or offline-critical.

```text
┌───────────────────────────────┐          ┌─────────────────────────────────┐
│ 💻 your desk — tauri app      │          │ 🌲 chordial server               │
│                               │          │                                 │
│  react + typescript ui        │◄────────►│  api + sync (accounts, wss)     │
│   home · rooms · cycles ·     │  rooms,  │  room runtime + council         │
│   deer window · timers        │ messages,│  llm gateway (keys, caching,    │
│                               │  nudges  │    metering — server-side ONLY) │
│  rust collectors              │          │  telegram bridge (the tether)   │
│   bundle id · idle · sanitize │          │  postgres — canonical shared    │
│   at the boundary             │ derived  │    reality (rooms, cycles,      │
│                               │ events ↑ │    events, observations,        │
│  python sidecar               │ views  ↓ │    memories — per user)         │
│   ambient engine · pulse ·    │          │        ▲                        │
│   sessions · gates · budget   │          │        │ telegram               │
│                               │          │   📱 her phone                  │
│  local sqlite                 │          │                                 │
│   cache + outbox · timer and  │          │                                 │
│   deer work offline           │          │                                 │
│                               │          │                                 │
│ 🔒 raw activity signals never │          │  multi-tenant from the start:   │
│    leave this box — only      │          │  per-user streams, usage        │
│    derived events cross;      │          │  metering, attention budgets,   │
│    no api keys in the app     │          │  prompt-cache discipline        │
└───────────────────────────────┘          └─────────────────────────────────┘
```

### the sync contract

Offline-first sync is only trustworthy if idempotency and ordering are explicit
— a focus block retried from the outbox after a dropped connection must never
count twice. Defined **before** the phase 1 API, enforced from the first
endpoint:

- **Every device-origin event carries a client-generated event UUID** plus
  `(device_id, seq)` — a per-device monotonic sequence stamped by the sidecar
  at write time.
- **The server applies idempotently**: unique constraints on the event UUID and
  on `(device_id, seq)`; a duplicate apply is a no-op that still ACKs.
- **ACKs are durable cursors**: the server acknowledges the highest
  contiguously-applied `seq` per device; the outbox trims at the cursor and
  retries everything after it. At-least-once delivery, exactly-once apply.
- **Rejections never pin the cursor.** A semantically invalid event (unknown
  type, bad payload shape) is persisted as a rejection *tombstone* that
  consumes its `(device, seq)` — the cursor passes it, the error rides in the
  row and the response. Only a structurally unparseable entry (no valid
  uuid/seq — a client bug) is response-only, since it has no sequence to
  consume.
- **Quota counts only newly inserted rows.** Deduplication happens before the
  cap check, so a device retrying already-applied events at the cap gets its
  durable ACK back instead of a 429 — dropped-response recovery must never
  deadlock on quota.
- **Ordering is per-device**, which is all the domain needs: device events are
  append-only facts (`focus_block.completed`, `drift.detected`) and never
  conflict with each other. Mutations of canonical rows (tasks, cycles) are not
  synced as blind writes — they go through the server-side store, which owns
  the invariants.

### multi-tenant invariants (from the first endpoint, not phase 7)

Tenancy is schema and API shape, not late-stage hardening. From phase 1:
every query is scoped by an authorized `user_id` (no ambient single-user
assumptions); devices hold individually revocable credentials, not a shared
secret; websocket connections are authenticated and scoped to room membership;
per-user quotas and token budgets are enforced at the gateway. What phase 7
keeps is *operational* maturity: load testing, metering dashboards, backups,
tuning.

### why the line is drawn there

- **LLM calls are server-only.** A distributed desktop binary can never contain
  API keys; the server gateway owns providers, prompt caching, model tiering,
  and per-user metering (the existing usage-log machinery generalizes).
- **Ambient must be instant and offline.** Drift detection at a 2-second poll
  and a ticking timer can't ride a round-trip. The sidecar runs the pulse
  engine locally with authored fallback lines for offline moments; generated
  ambient lines come from a server-batched nightly pool (unread ahead of time —
  surprise preserved, spend amortized).
- **Privacy is enforced by topology.** Raw signals are sanitized at the Rust
  boundary and consumed in-process; the server sees only derived events. This
  is the parent-trust story too: any future guardian report covers *process*
  (focus time, phase), never *content*.
- **Cost posture for 100+:** one speaker per turn, deterministic routing before
  any model call, small-model deciders, byte-stable cached prompt prefixes,
  bounded room contexts, per-user attention and token budgets. The council must
  be affordable *because* it's mostly silent.

### the box, as built (phase 7b)

One installable app — shell, frontend, and the sidecar frozen inside it —
plus a signed self-update feed. Runbook: `docs/PACKAGING.md`.

- **The sidecar is a PyInstaller onefile binary** (~10 MB: aiohttp + stdlib,
  no hiddenimports — the analysis IS the inventory, and the server's world
  stays structurally outside the box). `packaging/build_sidecar.sh` names it
  per tauri's externalBin convention. Frozen runs move state to the platform
  app-data dir (`src/sidecar/paths.py`, identifier pinned to
  tauri.conf.json by test) and log to `sidecar.log` beside the db; dev runs
  keep the cwd. Measured ~6–8s spawn-to-bind (onefile self-extraction) —
  every consumer already tolerates it.
- **Release builds own the sidecar's life; debug builds spawn nothing**
  (the four-terminal dev loop is untouched). The shell probes
  `/v1/state` first and ADOPTS a sidecar already on the port (second app
  launch, dev terminal) rather than fighting for it; spawns with
  `SIDECAR_DB` pointed at tauri's own app-data dir; respawns on death with
  a capped backoff ladder (a run that lived a stable minute resets it); kills
  the child on every exit path via the run-event hook.
- **The updater polls the chordial server itself** — `/app/updates/`
  (latest.json + bundles) served read-only when `APP_UPDATES_DIR` is set;
  single origin, no third-party host, and a misconfigured dir warns instead
  of taking the council down. Bundles are minisign-signed at build; the
  pubkey pinned in tauri.conf.json means a compromised feed can withhold
  but never forge. Quiet launch check (~15s, silent when current or
  unreachable) + a tray "check for updates" that answers either way; both
  ask before installing. The private key lives outside the repo
  (`~/.tauri/chordial-updater.key`) — backed up or updates die with it.
- **The device token graduates to the OS keychain** (macOS Keychain /
  Windows Credential Manager via the shell's `credential_*` commands) in
  packaged builds; dev keeps localStorage (an unsigned debug binary changes
  identity every rebuild — macOS would prompt for each). First packaged run
  migrates a localStorage-era token in and deletes the plaintext copy; a
  refusing keychain degrades to localStorage with a warning rather than
  bricking linking. The deer's cross-window change signal rides a bare rev
  counter in localStorage — the storage event still fires, no secret rides
  it. (The stronghold plugin noted earlier was passed over deliberately:
  it wants a password-derived snapshot; the OS keychain is the thing
  actually meant.)
- **Deferred, not faked:** Windows and linux boxes are compile-checked but
  unrun (collector still macOS-only); macOS code-signing/notarization —
  the .app runs locally unsigned, gatekeeper handling is 7c ops; presence
  from the deer window alone (deer-open-main-closed reads absent).

---

## 11. data & vocabulary

One canonical vocabulary, now that rooms make "conversation" the wrong frame.
Engine-side names that conflict get renamed at the chordial layer (and proposed
upstream to the dainframe as findings):

| term | meaning | notes |
|---|---|---|
| **room** | bounded space: purpose, participants, scoped context, lifecycle | new; each room is its own engine stream |
| **room log** | what was said and done in a room, with author + platform provenance | rename of "conversation events" — same store, per-room streams |
| **event** | a domain fact: `focus_block.completed`, `meal.logged`, `scope_change.created` | new table; never mixed with the room log |
| **observation** | a helper's structured, evidence-tied interpretation | new; confidence-scored, silent by default |
| **assessment** | higher-level claim with evidence links + suggested action | new; Edwin's findings live here |
| **cycle** | two-week rhythm: theme, capacity, commitments, guardrails | existing table, extended — *never "sprint"* |
| **focus block** | 25 minutes of effort, not a completion promise | existing focus tracking, now session-owned by the sidecar |
| **artifact** | durable interactive object born in conversation | new; typed rows rendered by React, degrade to text on the tether |
| **the tether** | the single-bot Telegram window into today's room | new |

Nearly everything else carries over untouched: plans, goals, tasks, wins,
check-ins, notes ("jot" is still the verb), occasions, memories, the curator,
the reconciler, usage metering, and the whole lifecycle convention. Helpers
propose; only the store mutates canonical state; Vel budgets all proactive
speech.

### the identity split (prerequisite for rooms)

The current runtime equates the engine stream with the user: `stream_id` *is*
`user_uuid` through helpers, tools, workspace queries, usage accounting,
profile lookup, delivery, and pulse scheduling. Rooms break that equation —
a room-scoped stream id would silently empty workspace queries and
misattribute usage. So **before any room migration**, `user_id` and
`stream_id` become separate, explicit fields threaded through `Stimulus`,
`Briefing`, `ToolContext`, and usage events: `user_id` answers "whose reality?"
(workspace, memories, budgets, delivery); `stream_id` answers "which
conversation?" (room log, prompt history). This is phase 2's first step and a
mechanical sweep — and a dainframe API finding to propose upstream (the
engine's `ToolContext`/usage vocabulary currently lets consumers conflate the
two).

Example structured records:

```yaml
event:
  id: 7c9e6679-7425-40de-944b-e07fc1f90ae7   # client-generated, dedup key
  source: { type: sidecar, device_id: dev_abc123, seq: 4182 }
  type: focus_block.completed
  payload: { commitment_id: cmt_9f2b41, minutes: 25 }

observation:
  agent: productivity
  fact: { type: commitment_progress, commitment: room_runtime_v1,
          completed_blocks: 2, planned_blocks: 5 }
  confidence: 1.0

assessment:
  agent: productivity
  claim: chordial work is progressing, but initiation concentrates late in the day
  evidence: [focus_block:123, focus_block:128, focus_block:141]
  confidence: 0.81
  suggested_action: try one chordial block before noon tomorrow
```

All helpers speak a shared runtime protocol — they return observations,
assessments, proposed actions, and requested speech. Helpers never directly
mutate canonical state or send arbitrary proactive messages.

---

## 12. building it

The unfair advantage: most of the runtime already exists. The engine chordial
runs on (the dainframe) treats a conversation as an opaque stream — rooms are
simply *many streams per user* instead of one. The workspace schema, single-bot
telegram plumbing, link codes, memory system, and cost guards all carry over.

| phase | scope | already have |
|---|---|---|
| **0 · scaffold** | this doc in the repo; `app/` created (tauri + react + ts) | ✓ decisions locked |
| **1 · server api** | grow the existing web server into the app-facing api: accounts + devices (individually revocable credentials), rooms, messages, authenticated websocket streaming scoped to room membership, read models (`today_view`, `cycle_view`); **the sync contract** (event UUIDs, per-device sequences, durable ACK cursors, server dedup constraints) and **tenant scoping + per-user quotas on every endpoint** — multi-user is API shape here, not phase-7 hardening; runs on the existing server box | ✓ http server, auth, link codes |
| **2 · rooms become streams** | **first: the identity split** — separate `user_id` from `stream_id` through `Stimulus`/`Briefing`/`ToolContext`/usage events (§11; today the runtime equates them everywhere); then rooms tables; stream id becomes room id (the current single stream is grandfathered as a legacy room); per-room-type context policies; the `open → closing → closed` lifecycle with non-blocking close (§4); **retire the event-log trim/cleanup path** (archives are forever) | ✓ engine + event store adapter |
| **3 · the council** | character definitions with per-user overrides; deterministic routing spine + decider; observations/assessments tables; helpers emit them | ✓ persona yaml + silent-agent pattern |
| **4 · the shell & the deer** | react app in dev mode: home, daily room, presence strip; port the antler mvp: deer window, rust collectors, sidecar ambient engine with local sqlite, focus sessions; no bundling yet — `tauri dev` + local python side by side | |
| **5 · the vertical slice** | cycle fields (theme, capacity) + commitment rows with stable ids + baseline snapshots + scope-change projection (§6); a completed focus block flows device → sync contract → pip observation → cycle view moves → next-action artifact. **the milestone:** enter today's room, do one block with the deer, watch shared state move, revisit the archived day | |
| **6 · edwin & the cycle rooms** | port the scorer pattern (proven in cadenza); scorecards; retrospective + planning room types; the arc's taper arithmetic starts accumulating honest data | ✓ 6a built: deterministic scorecard + evidence-checked findings + exactly-once filing (§8 as-built) · ✓ 6b built: retro + planning rooms, edwin presents the card (§4 as-built) · ✓ 6c built: the taper — steady scorecards stretch the check-in beat, capped at the sentinel (§7 as-built). **phase 6 complete** |
| **7 · the tether & the box** | single-bot telegram bridge with inline attribution + presence-aware routing (per-helper bot ensemble deleted, not ported); then packaging (pyinstaller sidecar bundle, updater) — deliberately last; then operational maturity: load testing, metering dashboards, backups, tuning (the multi-user *invariants* landed in phases 1–2) | ✓ 7a built: the tether — one bot, whole council, inline attribution, presence-aware routing, the desktop mirror (§9 as-built) · ✓ 7b built: the box — frozen sidecar spawned/supervised by the shell, signed self-update feed off the server itself, token in the OS keychain (§10 as-built, `docs/PACKAGING.md`) |

### explicitly deferred

Avatar animation beyond static expressions · user-created helper species ·
large dashboards · fully autonomous helper actions · dozens of room templates ·
deep behavioral analytics · drag-and-drop everywhere · native mobile app — the
eventual real phone story; the tether is the honest interim, not the
destination.

### retired along the way

The web focus view · the multi-bot telegram/discord ensemble (per-persona bot
handles, group chat mechanics) · the mandatory representation ritual (becomes
optional) · `chat_service`'s platform-adapter role beyond the single-bot tether.

### the v1 milestone

> a user can enter today's room, interact naturally, complete one focus block
> with the deer at her side, see the shared cycle state update, and later
> revisit the archived day — while a small council of characterful helpers
> participates coherently (which means: visibly listens, and occasionally one
> of them speaks).

---

## north star

**A personal world made of small collaborative spaces, inhabited by helpful
characters, with one shared memory beneath them** — cozy, playful, rigorous,
and quiet by default. It meets you loudly when you're finding your feet, works
alongside you while you build, and then earns the right to mostly hush: a small
woodland government that dreams of being needed less, and a deer who stays
either way. 🦌
