# chordial.ai

**chordial** is a proactive, memory-bearing ai companion — or rather, a small *ensemble* of them. a roster of helper characters shares one group chat with you, each with its own personality, specialty, and telegram bot. they remember you across months, reach out on their own schedule (politely — see the proactivity gate below), and follow you across platforms mid-conversation without losing the thread.

built and run solo. in continuous daily production use since june 2025.

chordial runs on [**the dainframe**](https://github.com/DainDeer/the-dainframe) — its orchestration machinery (ai providers, tool registry, agent loop; soon the engine and ambient pulse), extracted into its own framework with chordial as the first consumer. the dainframe is a **required dependency**: see [installing & running](#installing--running).

### the ensemble

| helper | specialty |
|---|---|
| **chordial** | general companion — reaches out the most |
| **tempo** | fitness & movement |
| **aria** | music |
| **pep** | productivity cheerleader & cycle balance-keeper |
| **mochi** | emotional support — never assigns work |
| **poet** | writing (songwriting is a deliberate poet↔aria duet) |

- archetypes are repo config ([src/personas/*.yaml](src/personas/)) — form-agnostic baseline personalities, each a cache-stable frozen prompt block
- *identities* are emergent per user: name, form, and vibe are chosen during a storytelling introduction ("you enter the chordial forest…") and persisted as shared memories + `HelperState`
- group chat is for shared presence; each helper is also its own private DM. DMs are **truly private** — never rendered into a sibling's context window. the cross-helper channel is memory: important DM facts become shared memories with attribution, so a sibling can say "i heard from aria that…"

### how a message flows

```
Stimulus (user message / scheduled tick / curation due / introduction)
  → ProactivityGate   (proactive ticks only — pure event-log arithmetic, zero tokens)
  → Orchestrator._direct  (the director: casts a Script of speakers, max 2)
  → per speaker: Briefing (event window + ambient context + profile)
      → agent.act(briefing) → AgentOutcome (tool calls + reply)
  → event log (actions recorded, then the reply)
  → delivery to the user's active platform
```

the director is rules-only today (DM → that helper, group @mentions → those helpers, else chordial); the next phase replaces the no-mention branch with a cheap utility-model call producing a multi-speaker script. agents implement a two-method protocol (`name` + `act`) — the chat helpers, the silent memory curator, and future personas all plug into the same seam.

after each user turn, a **completion reconciler** (utility-model pass) checks whether the user casually mentioned finishing an open task and closes it — the companion shouldn't need to be explicitly told.

### the event log (not "conversation history")

one conversation per **user**, not per platform. every event carries `author_type` / `author` / `kind` / `platform`, where platform is pure provenance — never a filter key. switch from discord to telegram mid-thought and the thread just continues (the abandoned platform gets one self-deduping *"pssst — we're chatting over on telegram now"*).

- `kind='action'` rows are executed tool calls, frozen into promptable one-liners at write time and replayed into the next turn — this eliminated the entire "model silently re-ran a tool call it already made" bug class
- `kind='note'` rows never render into prompts and never count as "the assistant replied"
- dm/group scope on each event powers the privacy windowing above

### proactivity without being annoying

the **proactivity gate** runs before any generation — a denied tick costs one db read and zero tokens. three stacked rules: a crew-wide cap on unanswered proactive messages (being ignored is a signal to *everyone*), a per-helper cap, and exponential backoff (3h → 6h → 12h). any user message anywhere resets all three. plus quiet hours, and delivery only to users past onboarding.

### prompt caching as a discipline

anthropic's prompt cache is a byte-exact prefix match, so prompts are built in frozen zones: tools → persona block → per-user profile (cache breakpoint) → history → volatile now-context. byte-stability is treated as a tested invariant — golden-bytes tests guard the persona block, and schema migrations are verified to render old histories byte-identically, so warm caches survived every refactor. one hard-won rule: only *user* turns get timestamp prefixes — prefixing assistant turns once taught the model to echo `[timestamp]` into real replies.

### the workspace

helpers share a workspace with the user: **plans → goals → tasks**, bi-weekly **cycles** as the balancing lever, **wins** (a ledger that quotes your own words back when you diminish an accomplishment), check-ins, notes, and occasions. an agenda snapshot gives every helper ambient awareness of your day.

fully postgres-native, in-db, always on (see [docs/NATIVE_WORKSPACE_DESIGN.md](docs/NATIVE_WORKSPACE_DESIGN.md)). notion is no longer a backend — its client survives only for a future one-way mirror.

### providers & platforms

- **ai:** anthropic primary (chat model with adaptive thinking + effort control; haiku as the utility model for the curator, reconciler, and director), openai supported behind the same interface. providers **raise typed errors** (`ProviderRateLimited`, `ProviderUnavailable`…) — never apology strings, and failures are never persisted as conversation events. the provider layer, tool registry, and agent loop are dainframe machinery; chordial supplies the personas, prompts, tools, and storage
- **cost guards:** tool-loop iteration cap, per-provider concurrency limiter, per-call usage logging and agent traces (chordial's `UsageRecorder` implements the dainframe's `UsageSink`)
- **platforms:** discord + telegram coexisting in one asyncio loop (telegram in manual lifecycle mode — the documented pattern for sharing a loop with discord.py). one telegram bot *per helper*. chat-first account linking: a single-use deep-link code, and unknown senders never touch the ai layer
- **storage:** postgres in production, sqlite for dev, alembic migrations throughout

### project structure

```
├── main.py                        # entry point & supervision
├── config.py                      # env config (models, helpers, gate knobs, flags)
├── src/
│   ├── agents/                    # Agent protocol: HelperAgent, CuratorAgent
│   ├── personas/                  # archetype cards (*.yaml)
│   ├── managers/                  # event log, users, memories, helper state
│   ├── providers/
│   │   └── platforms/             # discord, multi-bot telegram
│   ├── services/
│   │   ├── orchestrator.py        # stimulus → director → brief → act → record
│   │   ├── proactivity_gate.py    # the non-interaction guard
│   │   ├── tools/                 # chordial's tools (memory, workspace, linking…)
│   │   ├── workspace/             # native workspace store + agenda
│   │   └── notion/                # notion client (kept for a future one-way mirror)
│   └── database/                  # sqlalchemy models, engine setup
├── docs/                          # design docs — architecture & trade-offs
├── alembic/                       # schema migrations
└── tests/                         # ~370 tests: recording invariants, migrations, caching, gate…
```

the ai provider layer, tool registry/context, and agent loop live in [the dainframe](https://github.com/DainDeer/the-dainframe) and are imported from it; `src/managers/event_store_adapter.py` implements the dainframe's `EventStore` contract over chordial's database (proven by the library's conformance suite).

### installing & running

**requirements:** python ≥ 3.10, [poetry](https://python-poetry.org/), and a checkout of [the dainframe](https://github.com/DainDeer/the-dainframe) **as a sibling directory** of this repo.

the dainframe is consumed as a **path dependency** (`../the-dainframe`, editable) while its api is being extracted: `poetry install` fails without the sibling checkout. this is a deliberate dev-loop choice (the dainframe's [DESIGN.md](https://github.com/DainDeer/the-dainframe/blob/main/docs/DESIGN.md) §8), not an accident — once the library tags `v0.1.0`, the path dep becomes a pinned git tag and standalone checkouts work again.

```bash
# both repos, side by side
git clone https://github.com/DainDeer/chordial-mvp
git clone https://github.com/DainDeer/the-dainframe
cd chordial-mvp

# install (resolves dainframe from ../the-dainframe)
poetry install

# configure: create .env at the repo root (api keys, bot tokens, models,
# enabled helpers) — config.py documents every knob and its default

# run
poetry run python main.py

# tests
poetry run pytest
```

day-to-day implications of the editable path dep:

- **updating chordial:** `git pull` (+ `poetry install` if `poetry.lock` changed)
- **updating the dainframe:** `git pull` in `../the-dainframe` — no reinstall needed; the editable install imports straight from the checkout
- **ci / deploy:** any environment that installs chordial needs the same sibling clone first (the server layout mirrors this: `/home/dain/chordial-mvp` + `/home/dain/the-dainframe`; see [deploy/chordial-dev.service](deploy/chordial-dev.service))
- if the dainframe ever changes its *own* dependencies, chordial's lock goes stale — `poetry lock && poetry install` here, commit the lock

telegram bot setup (one bot per helper) is its own guide: [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md). database options (sqlite dev default, postgres) live in [docs/DEV_DATABASE.md](docs/DEV_DATABASE.md) and [docs/DATABASE_MIGRATIONS.md](docs/DATABASE_MIGRATIONS.md).

### design docs

the interesting decisions are written down: [V3_DESIGN.md](docs/V3_DESIGN.md) (the ensemble), [NATIVE_WORKSPACE_DESIGN.md](docs/NATIVE_WORKSPACE_DESIGN.md) (notion → postgres), [ACTION_RECONCILIATION_ENGINE.md](docs/ACTION_RECONCILIATION_ENGINE.md), [SECURITY_NOTES.md](docs/SECURITY_NOTES.md) (self-audit of user-input surfaces), and more in [docs/](docs/). the orchestration framework's own design — the engine contracts, the pulse, and the extraction plan — lives in [the dainframe's DESIGN.md](https://github.com/DainDeer/the-dainframe/blob/main/docs/DESIGN.md).

### status

shipping next: the ai director (utility-model script casting for the group chat), and the native workspace cutover.
