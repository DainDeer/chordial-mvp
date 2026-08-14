# chordial 🦌

**chordial is an ADHD productivity tool** — a body-double on your desktop, a gentle two-week structure around your work, and a small council of animal characters who hold the thread when your brain drops it.

built and run solo, by an ADHD brain, for daily use on the real thing. in continuous production since june 2025.

most productivity tools assume the hard part is deciding what to do. with ADHD the hard parts are **starting**, **staying**, and **coming back** — so those are what chordial is built around:

- **starting is one tap.** home shows one next action with a *start a block* button. no triage screen, no seventeen lists. activation energy is treated as a design constraint, not a user failing.
- **a body-double while you work.** a deer lives in a small always-on-top window — presence without demand, companionable silence as her default state. she holds the focus clock, dings once at the target, and throws confetti when you finish.
- **time banks; nothing is ever "abandoned".** pause a block and the minutes bank. switch tasks and the old clock banks. work past the ding and overtime counts *up*. no mechanic anywhere in chordial makes wandering attention feel like losing.
- **drift gets one soft check-in.** ten minutes idle mid-block earns at most one gentle word; an unanswered check-in is silently assumed to be a break and never re-asked. meetings hush her entirely.
- **changing your mind is data, not failure.** two-week **cycles** freeze a baseline of commitments; every scope change afterwards is an appended fact with your own reason attached — visible, diffable, unshamed. completing things is always ordinary and always allowed.
- **it fades, it doesn't leave.** as consistency becomes your own, the council quiets by design — routines that hold lose their nudges, and the end state is a sentinel: a quiet companion who still holds the door against the slide back.

the full design (and the reasoning behind it) is [docs/ROOMS_DESIGN.md](docs/ROOMS_DESIGN.md).

### the council

the space is inhabited by seven authored characters — each with its own voice, lane, and speaking policy, every name and species reshapeable in conversation ([src/personas/*.yaml](src/personas/), voices in [docs/COUNCIL_VOICE_REFERENCE.md](docs/COUNCIL_VOICE_REFERENCE.md)):

| resident | lane |
|---|---|
| 🦌 **vel** (chair) | orchestrator & ambient presence — holds the room, lives on the desktop, speaks for the house |
| 🐿️ **pip** | productivity — cycles, focus blocks, task breakdown |
| 🐰 **skip** | movement — five minutes counts |
| 🦝 **remy** | nutrition — approximate estimates, zero moralizing |
| 🐻 **mabel** | wellbeing — rest, comfort, the whole organism |
| 🦊 **juniper** | creative & hobbies — keeper of sparks, defender of unproductive joy |
| 🦉 **edwin** | reflection & scoring — speaks rarely, and it counts |

a rules-first director decides who speaks (a cheap utility-model call only for genuinely gray turns; one speaker per turn by default), and helpers **notice** things as structured observations — pip processes every completed focus block into cycle progress without being asked.

### the shape of the thing

client/server from day one ([docs/ROOMS_DESIGN.md](docs/ROOMS_DESIGN.md) §10):

- **the desktop app** ([app/](app/)) — a tauri shell: the main window (rooms, the cycle panel, the archive of remembered days) and the deer window. react + typescript, warm pastel "clearing" theme.
- **the sidecar** ([src/sidecar/](src/sidecar/)) — a loopback-only python engine that owns the focus clock against its own local sqlite. blocks survive restarts and work offline; completions pump to the server through an at-least-once outbox against exactly-once apply, so a block finished on a plane syncs when you land. **no llm keys on the device, ever** — every ambient word is authored text.
- **the server** — postgres, the event log, the council runtime, all llm calls, device auth, and sync. conversations happen in **rooms** (today's room, the archive) over one shared reality: cycles, commitments, tasks, observations, memories.
- **telegram** — a single bot keeps the thread when you're away from the desk.

### privacy as a load-bearing wall

the activity collector is ~100 lines of rust ([app/src-tauri/src/collector.rs](app/src-tauri/src/collector.rs)) and is *the* boundary: it reads only the frontmost app's bundle id and idle seconds — window titles, urls, and content are structurally absent, sanitized before anything reaches even the local engine. raw signals never leave the machine; only derived events (`drift.detected`, `focus_block.completed`) sync. the server's proactive voice is metered too: a zero-token proactivity gate, per-hour word budgets, quiet hours, and meeting hush — silence is the default gift.

### engineering notes

the parts that were hard-won, briefly:

- **the event log, not "conversation history".** one log per user across every platform and room; `platform` is provenance, never a filter key. executed tool calls are frozen into promptable one-liners at write time and replayed — which eliminated the entire "model silently re-ran a tool" bug class. archives are forever; there is deliberately no deletion path.
- **prompt caching as a tested invariant.** prompts build in frozen zones (tools → persona block → profile → history → volatile tail); golden-bytes tests guard the persona blocks, and migrations are verified to render old histories byte-identically, so warm caches have survived every refactor.
- **sync that trusts nothing.** device events carry client uuids and per-device monotonic seqs; the server applies exactly-once, tombstones the semantically-invalid so the ack cursor never pins, and a fresh client db under an existing device realigns instead of silently losing history. the invariants are pinned by tests with names like `test_fresh_store_under_an_existing_device_loses_nothing`.
- **typed provider errors** (`ProviderRateLimited`, `ProviderUnavailable`…) — never apology strings, and failures are never persisted as conversation events.

chordial runs on [**the dainframe**](https://github.com/DainDeer/the-dainframe) — the orchestration machinery (ai providers, tool registry, agent loop) extracted into its own framework with chordial as first consumer.

### project structure

```
├── main.py                 # entry point & supervision
├── config.py               # env config — documents every knob and default
├── app/                    # the desktop app: tauri shell, react frontend, deer window
├── src/
│   ├── sidecar/            # on-device focus engine: clock, outbox, gates, drift
│   ├── personas/           # the council's cards (*.yaml)
│   ├── agents/             # Agent protocol: helpers, the silent memory curator
│   ├── services/           # orchestration, rooms, cycles, sync, tools, workspace
│   ├── web/                # device auth, /api/v1, the sync contract
│   ├── providers/platforms/  # app, telegram, discord
│   └── database/           # sqlalchemy models, engine setup
├── docs/                   # design docs — architecture & trade-offs
├── alembic/                # schema migrations
└── tests/                  # ~590 python tests + vitest on the frontend
```

### installing & running

**requirements:** python ≥ 3.10, [poetry](https://python-poetry.org/), and a checkout of [the dainframe](https://github.com/DainDeer/the-dainframe) **as a sibling directory** (consumed as an editable path dependency while its api is being extracted — `poetry install` fails without it).

```bash
# both repos, side by side
git clone https://github.com/DainDeer/chordial-mvp
git clone https://github.com/DainDeer/the-dainframe
cd chordial-mvp

poetry install

# configure: create .env at the repo root (api keys, tokens, models) —
# config.py documents every knob and its default

# run the server
poetry run python main.py

# tests
poetry run pytest
```

the desktop app has its own dev runbook — sidecar, link codes, and the full four-terminal loop — in [app/README.md](app/README.md). database options live in [docs/DEV_DATABASE.md](docs/DEV_DATABASE.md) and [docs/DATABASE_MIGRATIONS.md](docs/DATABASE_MIGRATIONS.md); telegram setup in [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md).

### design docs

the interesting decisions are written down: [ROOMS_DESIGN.md](docs/ROOMS_DESIGN.md) (rooms, cycles, the council, the arc — authoritative), [COUNCIL_VOICE_REFERENCE.md](docs/COUNCIL_VOICE_REFERENCE.md) (who these characters are), [CREATIVE_COVENANT.md](docs/CREATIVE_COVENANT.md) (the ethics of a companion that helps you make things), [NATIVE_WORKSPACE_DESIGN.md](docs/NATIVE_WORKSPACE_DESIGN.md), [SECURITY_NOTES.md](docs/SECURITY_NOTES.md), and more in [docs/](docs/). the orchestration framework's own design lives in [the dainframe's DESIGN.md](https://github.com/DainDeer/the-dainframe/blob/main/docs/DESIGN.md).

### status

the v1 milestone is real and ran live end-to-end: one-tap block start → the deer's ding → sync → pip's observation → the cycle bar moving on its own → the day remembered in the archive.

shipping next: edwin's scorecard and retrospective/planning rooms (phase 6), then packaging, the telegram tether, and presence-aware routing (phase 7).
