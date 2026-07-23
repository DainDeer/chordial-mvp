# chordial.ai

**chordial** is a proactive, memory-bearing ai companion — or rather, a small *ensemble* of them. a roster of helper characters shares one group chat with you, each with its own personality, specialty, and telegram bot. they remember you across months, reach out on their own schedule (politely — see the proactivity gate below), and follow you across platforms mid-conversation without losing the thread.

built and run solo. in continuous daily production use since june 2025.

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

backed by notion today; a bespoke postgres-native backend is landing behind the `WORKSPACE_BACKEND` flag (schema and store merged, tools and live agenda in flight — see [docs/NATIVE_WORKSPACE_DESIGN.md](docs/NATIVE_WORKSPACE_DESIGN.md)).

### providers & platforms

- **ai:** anthropic primary (chat model with adaptive thinking + effort control; haiku as the utility model for the curator, reconciler, and director), openai supported behind the same interface. providers **raise typed errors** (`ProviderRateLimited`, `ProviderUnavailable`…) — never apology strings, and failures are never persisted as conversation events
- **cost guards:** tool-loop iteration cap, global concurrency semaphore, per-call usage logging and agent traces
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
│   │   ├── ai/                    # anthropic + openai behind one interface
│   │   └── platforms/             # discord, multi-bot telegram
│   ├── services/
│   │   ├── orchestrator.py        # stimulus → director → brief → act → record
│   │   ├── proactivity_gate.py    # the non-interaction guard
│   │   ├── tools/                 # tool registry (memory, workspace, linking…)
│   │   ├── workspace/             # native workspace store + agenda
│   │   └── notion/                # notion client + agenda snapshots
│   └── database/                  # sqlalchemy models, engine setup
├── docs/                          # design docs — architecture & trade-offs
├── alembic/                       # schema migrations
└── tests/                         # ~400 tests: agent loop, migrations, caching, gate…
```

### design docs

the interesting decisions are written down: [V3_DESIGN.md](docs/V3_DESIGN.md) (the ensemble), [NATIVE_WORKSPACE_DESIGN.md](docs/NATIVE_WORKSPACE_DESIGN.md) (notion → postgres), [ACTION_RECONCILIATION_ENGINE.md](docs/ACTION_RECONCILIATION_ENGINE.md), [SECURITY_NOTES.md](docs/SECURITY_NOTES.md) (self-audit of user-input surfaces), and more in [docs/](docs/).

### status

shipping next: the ai director (utility-model script casting for the group chat), and the native workspace cutover.
