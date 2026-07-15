# Native migration plan: SQLite → Postgres → native workspace

*Drafted 2026-07-13. The execution plan that takes the current codebase to
fully native storage: the app's database moves to Postgres, then the
workspace moves out of Notion into that database. The **what** of the
workspace build lives in `NATIVE_WORKSPACE_DESIGN.md` (revised 2026-07-13:
eight tables incl. notes/occasions, lifecycle convention, last_activity_at);
the Postgres rationale lives in `MULTI_USER_SPEC.md` §5 phase 0. This doc is
the **how and in what order**.*

---

## 0. Shape of the train

```
P  Postgres migration        app data moves; Notion untouched     2–4 days
A  workspace schema & store  8 tables born on Postgres            4–5 days
B  workspace tools & agenda  WORKSPACE_BACKEND gate, digest v2    4–5 days
C  Notion import & cutover   dainframe → native; flag flips       2–3 days
D  burn the boats            Notion code + snapshot table deleted 1–2 days
```

**Total ~3–4 weeks.** Postgres goes *first*, deliberately: the workspace
tables are then born on their final home, the one-time Notion import runs
exactly once against the real database, and the new alembic revisions are
written dialect-clean from day one instead of retrofitted. The two risky
moments (P cutover, C cutover) are kept **at least a week apart** so a
problem in one is never confounded with the other.

---

## 1. Current state (verified against the codebase, 2026-07-13)

- **Engine:** sync SQLAlchemy (`src/database/database.py`), sessionmaker,
  `DATABASE_URL` default `sqlite:///chordial.db` (`config.py:185`). The only
  sqlite-conditional code is the WAL pragma hook and `connect_args`, both
  already gated on `_IS_SQLITE`.
- **Schema authority:** Alembic (5 revisions, `render_as_batch=True`);
  `init_db()` runs `upgrade head` at startup — deploys self-migrate.
- **Runtime SQL:** all ORM. A grep for raw SQL / dialect functions in `src/`
  comes back clean — the app itself needs zero query changes for Postgres.
- **Known dialect landmine (migration history, not runtime):**
  `a68de0c288b5` drops the `compressed_messages` FK *by its
  naming-convention name*, which only exists because sqlite batch mode
  recreated the table. On Postgres that constraint would carry an auto-name
  (`compressed_messages_conversation_history_id_fkey`), so **replaying the
  revision chain on an empty Postgres is not guaranteed to work**. §2.3
  routes around this rather than patching history.
- **Datetimes:** naive UTC everywhere (documented at the top of
  `models.py`). JSON columns used widely (portable: SQLAlchemy `JSON` maps
  to pg `JSON`).
- **Dependencies:** poetry; sqlalchemy ≥2.0.41, alembic ≥1.16.2, **no
  Postgres driver installed yet**.
- **Backups:** ad-hoc file copies in `backups/`.
- **Process shape:** one asyncio process (`main.py`) — unchanged by this
  plan; Postgres is a URL swap plus ops, not an architecture change.

---

## 2. Phase P — Postgres migration

### 2.1 Provision

One Postgres 16+ instance per environment, database `chordial` (and
`chordial_dev` locally):

- **Local (macOS):** `brew install postgresql@16 && brew services start
  postgresql@16`, or Docker (`docker run -d --name chordial-pg -e
  POSTGRES_PASSWORD=… -p 5432:5432 -v chordial-pg:/var/lib/postgresql/data
  postgres:16`). Either is fine; pick one and note it in the README.
- **Server (wherever the app runs):** same-host Postgres via the distro
  package or Docker is plenty at this scale; a managed instance
  (Neon/Railway/Fly) is the zero-ops alternative. Decision deferred to
  deploy time — nothing in this plan depends on it.

### 2.2 Driver & engine

- `poetry add "psycopg[binary]"` — psycopg **3**, SQLAlchemy 2.0's modern
  dialect. URL form: `postgresql+psycopg://user:pass@host/chordial`.
- `database.py` deltas (small): pass `pool_pre_ping=True` (and default pool
  sizing) on the non-sqlite branch — the app runs for days at a time and
  idle connections die; pre-ping makes that invisible. Everything else
  (`_IS_SQLITE` gates) already does the right thing.
- No async engine. The codebase's sync-sessions-in-async-handlers pattern
  is a deliberate choice (queries are microseconds); switching to asyncpg
  is a refactor this migration does not need.

### 2.3 Schema creation on Postgres: `create_all` + `stamp head`, not chain replay

Because of the §1 landmine (and because replaying sqlite-batch-shaped
history on another dialect buys nothing), the fresh Postgres schema comes
from the **models**, which are the declared source of truth
(`DATABASE_MIGRATIONS.md`): run `Base.metadata.create_all()` against the
empty pg database, then `alembic stamp head` — the documented one-time
transition for a database that already matches the schema. From then on the
chain moves forward normally: `init_db()`'s `upgrade head` is a no-op until
the next real revision, which applies cleanly on both dialects.

*Rejected alternative:* dialect-gating the bad `drop_constraint` inside
`a68de0c288b5` so the chain replays on pg. Works, but edits migration
history to support a replay that will never happen again — `create_all` +
`stamp` is less code and uses an already-documented mechanism.

**From phase A onward, every new revision must be dialect-clean** (no
sqlite-only server defaults, no batch-mode assumptions) and is tested
against both engines (§2.7).

### 2.4 Data copy: `scripts/migrate_sqlite_to_postgres.py`

A ~180-line one-shot, same spirit as the Notion importer:

1. Connect to both URLs (source sqlite explicit, target pg explicit — never
   inferred from env, so a misconfigured `DATABASE_URL` can't invert the
   copy). Require the **source to be at this checkout's alembic head**
   (create_all builds the current models' schema; stamping it with an older
   source revision would lie and break the next startup upgrade). Refuse to
   run unless the target's tables are empty (`--force` truncates and
   restarts after a failed attempt).
2. Walk `Base.metadata.sorted_tables` (FK-safe order: users first), stream
   rows via SQLAlchemy Core, bulk-insert **preserving primary keys**.
   Naive datetimes and JSON columns round-trip as-is.
3. Reset sequences: for every integer-PK table,
   `SELECT setval(pg_get_serial_sequence('<t>','id'), max(id))` — skipping
   string-PK tables (`users.uuid`). Missing this = duplicate-key errors on
   the first insert after cutover; it's the classic pg-migration bug, hence
   a named step.
4. Verify **inside the same target transaction, before commit**: full-row
   comparison of every table ordered by primary key (the db is small enough
   that comparing content costs nothing and proves more than counts). Any
   mismatch rolls the whole copy back and exits nonzero — the target is
   never left holding unverified data.

### 2.5 Cutover: preflight gates, then runbook (Dain's instance)

**Preflight gates — all four green before starting the runbook** (these are
prerequisites, not follow-ups; the sqlite "the db is just a file" safety net
disappears at cutover, so its replacements must already exist):

- [ ] **pg backup timer installed and rehearsed**: the nightly `pg_dump`
  job (§2.6) exists, has produced at least one dump, and that dump has been
  **restored somewhere else** (`createdb restore_test && pg_restore …`) —
  an unrestored backup is a hope, not a backup.
- [ ] **Test suite green on Postgres** via the §2.7 lane.
- [ ] **Source at alembic head** (`alembic current` on sqlite) — the copy
  script enforces this anyway.
- [ ] **Rehearsal copy** against `chordial_dev` verified green.

**Runbook:**

1. Stop the app. Take the final sqlite backup **WAL-safely**:
   `sqlite3 chordial.db ".backup backups/chordial-final-precutover.db"` —
   a plain file copy can miss committed transactions still living in
   `chordial.db-wal`; the `.backup` command folds them in.
2. Run the copy script against the real target (it does create_all + stamp +
   copy + verify-in-transaction in one pass).
3. Point `DATABASE_URL` at Postgres; start the app.
4. Smoke: send a chat message (event log write), recall a memory, let a
   scheduler tick pass, `alembic current` shows head.
5. **Rollback lever:** repoint `DATABASE_URL` at the sqlite file — valid
   until meaningful new writes land on pg (realistically: same-day). The
   frozen sqlite file is kept indefinitely; it just stops being current.

### 2.6 Backups

Nightly `pg_dump -Fc` via cron/systemd timer to `backups/` (rotate 14),
replacing file copies. Per §2.5 this is a **cutover prerequisite**: timer
installed, one dump taken, and one restore rehearsed before the runbook
starts.

### 2.7 Tests & CI

- Unit tests stay on sqlite (fast, zero setup) — the ORM-only codebase makes
  this a safe proxy for logic.
- The **pg lane** is built into `tests/conftest.py`: set
  `TEST_DATABASE_URL=postgresql+psycopg://…/chordial_test` and the suite
  runs against Postgres (schema dropped, rebuilt from models, and stamped
  at head each session; the db name must contain "test"). Run it locally
  before any cutover and wire it into CI with a `postgres:16` service when
  CI exists. Once phase A lands, new alembic revisions get applied to a
  stamped pg database as part of this lane — the guard that keeps
  "dialect-clean" true instead of aspirational.

**Phase P estimate: 2–4 days** (matches MULTI_USER_SPEC §5), most of it in
2.4's verification and 2.7's lane, not the engine swap.

---

## 3. Phases A–D — the native workspace build

Fully specified in `NATIVE_WORKSPACE_DESIGN.md` §§2–10; deltas and
sequencing notes only:

- **A — schema & store (4–5 days):** the eight tables (`plans`, `goals`,
  `tasks`, `cycles`, `wins`, `checkins`, `notes`, `occasions`) in
  `models.py`, one alembic revision (born dialect-clean, applied to pg as
  its first real post-stamp migration — a good canary), `vocab.py`,
  `WorkspaceStore` with the §2.0 lifecycle invariants (`closed_at`
  stamping, open-by-default filters), `last_activity_at` side-effect
  stamping, occasion recurrence roll-forward. Store tests.
- **B — tools & agenda (4–5 days):** `workspace_tools.py` (9 preserved
  contracts + goals/wins/check-ins + `jot`/`list_notes`/`update_note` +
  occasions tools), `agenda.py` (digest v2 incl. occasions-within-3-days;
  notes never in agenda), reconciler payload swap, `WORKSPACE_BACKEND`
  gate, persona allowlists (mochi: read-only + `jot` + `log_occasion`).
  Deploy note: tool-definition bytes change ⇒ one prompt-cache break,
  routine.
- **C — import & cutover (2–3 days):** importer with `--dry-run` yaml
  review → `--apply`, idempotent on `notion_page_id` provenance (all
  imported entities, including `--import-bodies` notes). Runbook per design
  doc §6 — **the app is stopped** for import + flag flip, not merely left
  unchatted. `WORKSPACE_BACKEND=notion` remains the rollback lever, but be
  honest about its window: it's clean only until the **first native
  workspace write**; after that, rolling back means hand-replaying native
  task/plan changes into Notion (tractable — list rows with
  `updated_at > cutover` — but manual). Practically: decide within the
  first day or two, not the full week the code sticks around.
- **D — burn the boats (1–2 days, ~a week after C):** delete
  `src/services/notion/`, `notion_tools.py`, snapshot machinery +
  `agenda_snapshots` drop revision, `NOTION_*` config, old tests/docs;
  write `docs/WORKSPACE.md`; drop `*_project` aliases alongside the persona
  prompt update (one deploy).

---

## 4. Sequencing & gating

1. **P before A** — reasons in §0. A and B can start the day after P's
   cutover proves stable (they're additive; they don't touch P's tables).
2. **A–B are independent of the v3 launch train** (disjoint files, per the
   design doc). Run in parallel if the trains don't share a keyboard;
   sequentially P→A→B otherwise.
3. **C waits for the v3 helpers to exist and be introduced** — the
   importer's plan→steward assignments (`music→aria`, `Writing→poet`, …)
   land better when the stewards are real. This is the one hard gate.
4. **One cutover at a time:** P's rollback window (sqlite repoint) should be
   *closed* — i.e., pg stable for ≥a week — before C flips
   `WORKSPACE_BACKEND`. Never be in two rollback windows at once.

---

## 5. Rollback matrix

| moment | lever | window closes when |
|---|---|---|
| P cutover | repoint `DATABASE_URL` at frozen sqlite file | meaningful new writes on pg (~same day); file kept forever regardless |
| A–B deploys | revert the deploy; additive migrations sit unused | n/a — nothing consumes the tables until C |
| C cutover | `WORKSPACE_BACKEND=notion` (Notion left frozen, not deleted) | **first native workspace write** — clean rollback before it; manual replay of native changes into Notion after it (code itself survives until phase D, ~1 week) |
| D | git revert + the pre-D backup of `agenda_snapshots` drop | it's a deletion of already-dead code; lowest-risk phase |

---

## 6. Decisions recorded (so they aren't relitigated)

- **Naive UTC stays naive** (`TIMESTAMP WITHOUT TIME ZONE`). Consistent
  with every model and comparison in the codebase; a tz-aware refactor is
  orthogonal and not worth coupling to a data migration.
- **Sync engine stays.** No asyncpg, no async sessions.
- **`create_all` + `stamp head`** for the pg schema, not chain replay (§2.3).
- **Unit tests stay on sqlite**, with a pg lane as the dialect guard (§2.7).
- **No `TaskStore` protocol.** `WORKSPACE_BACKEND` is a transition flag
  with a scheduled death (design doc §7), not an abstraction.
- **Single process unchanged.** Worker splits, webhooks, quotas, invite
  gates are MULTI_USER_SPEC phases 0/2 concerns, deliberately not bundled
  into this train.

## 7. Risks

| risk | mitigation |
|---|---|
| sequence reset missed → duplicate-key crashes post-cutover | named step §2.4.3 + verification exits nonzero |
| copy script inverts source/target | explicit URLs, empty-target check, no env inference |
| new alembic revisions quietly sqlite-only | pg CI lane (§2.7); phase-A revision is the canary |
| idle-connection drops after days-long uptime | `pool_pre_ping=True` (§2.2) |
| both cutovers wobble at once | §4.4 one-rollback-window-at-a-time rule |
| backup gap after leaving "the db is a file" | pg_dump timer ships *in* the P cutover (§2.6) |
