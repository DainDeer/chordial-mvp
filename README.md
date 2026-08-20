# chordial 🦌

**chordial is an ADHD productivity tool** — a body-double on your desktop, a gently imposed two-week structure around your work, and a cast of helpers who assist with different areas of life!

chordial is built by one person with ADHD, primarily because i wanted this thing to exist for myself. i use it on my actual work every day!

most productivity tools assume the hard part is deciding what to do. with ADHD the hard parts are **starting**, **staying**, and **coming back** — so those are what chordial is built around:

- **starting is one tap.** chordial keeps the next action obvious and makes starting a focus block immediate. simple interactions and conversations handle the task and goal tracking around it.
- **a body-double while you work.** chordial lives in a small always-on-top window — presence to help the user stay on task without being too deerstracting.
- **time banks; nothing is ever "abandoned".** pause a block and the minutes bank. switch tasks and the old clock banks. work past the ding and overtime counts *up*. no mechanic anywhere in chordial makes wandering attention feel like losing.
- **built to not be annoying.** ten minutes idle mid-block gets a tiny ear perk and one task nudge. breaks are encouraged, but productivity is reinforced and rewarded!
- **provides structure, while keeping nothing set in stone** two-week **cycles** freeze a baseline of commitments; every scope change afterwards is an appended fact with your own reason attached. a separate scoring helper looks back at the cycle, identifies patterns, and gives targeted, sometimes adversarial, but always constructive advice.
- **naturally quiets as you grow into your better self** as your work habits become more consistent and need less guidance, chordial quiets by design — mainly existing as a safeguard to nudge you when you start returning to old stagnating habits.

the full design (and the reasoning behind it) is [docs/ROOMS_DESIGN.md](docs/ROOMS_DESIGN.md).

### a friendly cast of helpers

i've found that progress and personal development are much harder when tackled completely alone.

human connection is paramount, and chordial will encourage you to seek out real people, communities, professionals, and other resources whenever they can help. but we're all fallible creatures with limited time and energy, and nobody can be available for everyone all the time.

chordial's cast of helpers fills in some of those gaps, offering different perspectives across work, health, creativity, reflection, and everyday life, 24/7. because life is not only about work and productivity, after all :3

| resident | lane |
|---|---|
| 🦌 **vel** | orchestrator & ambient presence — lives on the desktop, acts your contact for whatever help you need in the moment |
| 🐿️ **pip** | productivity — cycles, focus blocks, task breakdown |
| 🐰 **skip** | movement — simple reminders to stand or move, while also optionally providing support for fitness goals |
| 🦝 **remy** | nutrition — effortless approximate calorie tracking for kind tracking of personal weight and body goals |
| 🐻 **mabel** | wellbeing — self-compassion, rest, and comfort are necessary for all but especially my fellow neurodivergent & ADHD folks |
| 🦊 **juniper** | creative & hobbies — helps to spark the joy that comes from cultivating hobbies centered on creating, practicing, or doing rather than consuming |
| 🦉 **edwin** | reflection & scoring — speaks rarely, but helps you notice patterns that may be working against the goals you've set for yourself. edwin doesn't tell you who to be or what to change. you decide that. he offers grounded retrospective advice and small, realistic things to try next time.  |

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

the desktop app has its own dev runbook — sidecar, link codes, and the full four-terminal loop — in [app/README.md](app/README.md). building the installable app (frozen sidecar, signed update feed) is [docs/PACKAGING.md](docs/PACKAGING.md); running it for real (metering dashboard, token budgets, backups) is [docs/OPERATIONS.md](docs/OPERATIONS.md). database options live in [docs/DEV_DATABASE.md](docs/DEV_DATABASE.md) and [docs/DATABASE_MIGRATIONS.md](docs/DATABASE_MIGRATIONS.md); telegram setup in [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md).

### design docs

the interesting decisions are written down: [ROOMS_DESIGN.md](docs/ROOMS_DESIGN.md) (rooms, cycles, the council, the arc — authoritative), [COUNCIL_VOICE_REFERENCE.md](docs/COUNCIL_VOICE_REFERENCE.md) (who these characters are), [CREATIVE_COVENANT.md](docs/CREATIVE_COVENANT.md) (the ethics of a companion that helps you make things), [NATIVE_WORKSPACE_DESIGN.md](docs/NATIVE_WORKSPACE_DESIGN.md), [SECURITY_NOTES.md](docs/SECURITY_NOTES.md), and more in [docs/](docs/). the orchestration framework's own design lives in [the dainframe's DESIGN.md](https://github.com/DainDeer/the-dainframe/blob/main/docs/DESIGN.md).
