# Native Workspace: bespoke data model replacing Notion

*Drafted 2026-07-12. Companion to `MULTI_USER_SPEC.md` (§3 Option B/C) and
`V3_DESIGN.md` (§5, the Plans/Goals/Wins schema). This doc is the full
implementation plan for moving the task workspace out of Notion and into
chordial's own database. All user-interface work is explicitly stubbed —
chat remains the only surface.*

---

## 0. Summary & goals

**Goal:** chordial's own DB becomes the system of record for the user's
workspace (plans, goals, tasks, cycles, wins, check-ins). The Notion
integration is retired after a one-time import of the dainframe.

**Strategy in one line:** implement the *v3* schema natively (not the legacy
dainframe shape), keep the model-facing tool contract byte-compatible where
it exists today, collapse the agenda-snapshot caching machinery into live
queries, and cut over via an interactive import script.

**Non-goals (this doc):**
- Any web/graphical UI (stubbed — see §9).
- Google Calendar or any other external sync.
- Billing/quotas/multi-user hardening (MULTI_USER_SPEC phases 0/2).
- The evening "wins replay" *conversation design* and pep's cycle balance
  pass — those are prompt/orchestration features that consume this data
  model; they land separately. This doc only guarantees the data and tools
  they need exist.

**Why the v3 schema and not a 1:1 dainframe port:** the Notion-v3 redesign
(V3_DESIGN §5) was already approved and was going to force a second schema
build + fleet migration in Notion. Building bespoke means we design once:
the native tables *are* Plans/Goals/Tasks/Cycles/Wins/Check-ins from day one,
and legacy Projects become Plans at import time.

---

## 1. Current Notion touchpoints (what gets replaced)

| Component | Today | Fate |
|---|---|---|
| `src/services/notion/client.py` | httpx wrapper, retries, pagination | kept **only** inside the import script; deleted after cutover |
| `src/services/notion/schema.py` | dainframe property builders/readers, vocabularies | replaced by `src/services/workspace/vocab.py` + plain SQLAlchemy models |
| `src/services/notion/snapshot_service.py` | cached agenda (TTL + `invalidate_all()` staleness dance) | replaced by `workspace/agenda.py` — live queries, no cache, no staleness |
| `src/services/tools/notion_tools.py` | 9 tools (list/create/update × tasks/projects/cycles), title→id resolution | replaced by `tools/workspace_tools.py` — same tool *names and input contracts* for the 9, plus new v3 tools |
| `CompletionReconcilerService` | reads open tasks from the snapshot payload; marks Done via the `update_task` tool | **unchanged** except `_open_tasks` reads from `agenda.get_payload()`'s new implementation; the tool contract it executes against is preserved |
| `AgendaSnapshot` table | cached payload+digest per user | dropped (alembic) |
| `Config.NOTION_*` | key, db ids, api version, page cap | deleted; `WORKSPACE_BACKEND` gates the transition (§7) |
| `scripts/notion_smoke_test.py`, `NOTION_SETUP.md`, `docs/NOTION_INTEGRATION.md` | setup/verification | deleted / replaced by a short `docs/WORKSPACE.md` |
| tests: `test_notion_tools.py`, `test_agenda_snapshot.py` | mock Notion JSON | rewritten against native store (simpler — real DB fixtures, no property-JSON mocks) |

Everything else (orchestrator, agents, prompt zones, event log, scheduler)
is untouched: they consume the workspace only through the tool registry and
`get_digest()/get_payload()`, which keep their signatures.

---

## 2. Data model

New tables in `src/database/models.py`, one alembic revision. Conventions
follow the existing models: integer PKs (`sqlite_autoincrement`), `user_uuid`
FK on every table, `JSON` columns for sqlite/pg compatibility (the `Memory`
precedent), naive-UTC `created_at`/`updated_at`.

All controlled vocabularies live in **`src/services/workspace/vocab.py`** —
one module, same philosophy as `schema.py` ("encoded once"): DB stores
canonical lowercase-snake values; the tool layer accepts legacy display
strings case-insensitively ("To do", "In progress") and renders the display
form, so the model-facing vocabulary doesn't change.

### 2.1 `plans` (evolves Projects — V3_DESIGN §5.1)

| column | type | notes |
|---|---|---|
| id | int PK | |
| user_uuid | str FK users, indexed | |
| title | str, not null | |
| helper | str, not null | archetype id (chordial/tempo/aria/pep/mochi/poet) — steward |
| status | str | `proposed` / `active` / `paused` / `complete` |
| why | text, null | user's own motivation, their words |
| success_criteria | text, null | "success looks like" |
| horizon_start / horizon_end | date, null | soft range |
| cadence | str, null | `daily` / `weekly` / `loose` |
| legacy_area | str, null | preserved dainframe `Area` (audit/formatting nicety) |
| notion_page_id | str, null | provenance from import; never used at runtime |
| created_at / updated_at / archived_at | datetime | archived = soft delete |

### 2.2 `goals` (V3_DESIGN §5.2)

| column | type | notes |
|---|---|---|
| id, user_uuid | | |
| plan_id | int FK plans, not null | |
| title | str, not null | |
| status | str | `not_started` / `in_progress` / `done` / `renegotiated` |
| target | date, null | |
| done_means | text, null | the anti-vagueness field |
| created_at / updated_at | | |

### 2.3 `tasks` (evolves the dainframe Tasks db in place — V3_DESIGN §5.3)

| column | type | notes |
|---|---|---|
| id, user_uuid | | |
| title | str, not null | |
| status | str | `todo` / `in_progress` / `done` / `deprioritized` |
| priority | str, null | `high` / `medium` / `low` |
| scheduled | date, null | **user-local calendar date** (as Notion stored it); agenda comparisons use the user's `today`, exactly like `snapshot_service` does now |
| window | str, null | `morning` / `afternoon` / `evening` / `anytime` |
| pom_estimate | float, null | |
| plan_id | int FK plans, null | direct link (legacy tasks had Project but no Goal) |
| goal_id | int FK goals, null | when set, manager enforces `goal.plan_id == plan_id` |
| cycle_id | int FK cycles, null | the Sprint relation, singular |
| helper | str, null | who assigned/nudges |
| reschedules | int default 0 | bumped on each `scheduled` slip; renegotiate at 2–3 |
| description | text, null | |
| completed_at | datetime, null | set when status→done (Wins/analytics need it) |
| notion_page_id | str, null | import provenance |
| created_at / updated_at | | |

Indexes: `(user_uuid, status)`, `(user_uuid, scheduled)` — the agenda's two
query shapes.

**Simplification vs Notion:** Project/Sprint were multi-relations; every
consumer already takes only the first (`task_row`, `format_task`). Native
FKs are singular; the importer takes the first relation and logs any extras.

### 2.4 `cycles` (kept — the balancing lever)

| column | type | notes |
|---|---|---|
| id, user_uuid, title | | |
| status | str | `upcoming` / `active` / `complete` |
| start_date / end_date | date | |
| goal | text, null | today's "cycle goal" |
| focus | text, null | v3: pep's negotiated balance statement |
| created_at / updated_at | | |

### 2.5 `wins` (new — the anti-diminishment ledger)

| column | type | notes |
|---|---|---|
| id, user_uuid, title | | past-tense, concrete |
| date | date, not null | |
| helper | str, not null | who witnessed/logged it |
| plan_id | int FK plans, null | |
| task_id | int FK tasks, null | bonus over the Notion design: a win born from a completion keeps the link |
| evidence | text, null | the user's words verbatim at the time |
| weight | str | `spark` / `solid` / `milestone` |
| created_at | | |

### 2.6 `checkins` (new — the shared daily journal)

| column | type | notes |
|---|---|---|
| id, user_uuid | | |
| date | date, not null | |
| kind | str | `morning` / `evening` / `adhoc` |
| energy | str, null | `low` / `ok` / `good` / `great` — asked, never demanded |
| notes | text, null | |
| plan_ids | JSON, default [] | "plans touched" — JSON list, not an association table (promptable, sqlite-friendly; same precedent as `Memory.embedding`) |
| helper | str, not null | who ran it |
| created_at | | |
| *unique* | `(user_uuid, date, kind)` for morning/evening | adhoc unlimited |

### 2.7 Public IDs

Notion exposed UUIDs; the model mostly resolved things by *title* and echoed
ids opaquely (reconciler included). Native formatters render prefixed ids —
`t42`, `p3`, `g7`, `c2`, `w15`, `ci4` — and the tool layer parses them
(`_parse_public_id` replaces `_looks_like_id`). The reconciler round-trips
whatever id string appears in the open-tasks payload, so it works unchanged.

---

## 3. Package layout

```
src/services/workspace/
    __init__.py         # get_store() etc.
    vocab.py            # vocabularies, display<->canonical maps, format_* one-liners
    store.py            # WorkspaceStore: all queries/mutations (the one write path)
    agenda.py           # digest + payload, live queries (replaces snapshot_service)
src/services/tools/
    workspace_tools.py  # tool defs + handlers (replaces notion_tools.py)
scripts/
    import_notion_workspace.py   # one-time dainframe import (§6)
```

`WorkspaceStore` follows the managers pattern (`helper_state_manager` et al):
plain methods over `get_db()` sessions, sync (queries are microseconds; no
reason to fake async for sqlite/pg row reads — tool handlers stay `async` and
just call it). Every mutation goes through the store — tools, reconciler,
importer — so invariants (goal/plan consistency, `completed_at` stamping,
reschedule bumps) live in exactly one place.

`format_task` / `format_plan` / `format_cycle` / `format_win` in `vocab.py`
keep the same one-line promptable style as `schema.py`'s formatters
(`"title" [status] prio=… plan=… scheduled=… id=t42`), so prompt-facing
output changes minimally.

---

## 4. Tool surface

### 4.1 Preserved contracts (the 9 existing tools)

`list_tasks`, `create_task`, `update_task`, `list_projects`,
`create_project`, `update_project`, `list_cycles`, `create_cycle`,
`update_cycle` keep their **names and input schemas** (status/priority
display vocab, name-or-id references, list caps via
`WORKSPACE_MAX_PAGE_SIZE`, formerly `NOTION_MAX_PAGE_SIZE`). Two deltas:

- The `*_project` tools operate on **plans** under the hood; their
  descriptions change to say "plan"; `list_projects`/`create_project`/
  `update_project` get aliases `list_plans`/`create_plan`/`update_plan` with
  the projects-named versions dropped in the same release the personas'
  prompts are updated (one deploy — see cache note in §7).
- `create_task`/`update_task` inputs grow the v3 optionals: `window`,
  `goal`, `helper`. `update_task` bumping `scheduled` to a later date
  auto-increments `reschedules`.

Name→id resolution is reimplemented as SQL with the **resolution ladder**
semantics (fixed in `notion_tools.py`, 2026-07-12 — port the ladder, not the
old behavior): exact match, then case-insensitive exact, then substring —
first tier with any matches decides; a unique match resolves, several
matches return the candidates (title + id) as the tool result instead of
guessing, zero fall through. In SQL: `title = :x`, then
`lower(title) = lower(:x)`, then `LIKE`, each returning *all* matches.

`Tool.record_event` flags carry over: `list_*` are pure reads (False),
mutations are True. `Tool.terminal` stays as-is per tool.

### 4.2 New v3 tools

| tool | inputs (sketch) | notes |
|---|---|---|
| `create_goal` / `update_goal` / `list_goals` | plan (name/id), title, status, target, done_means | |
| `log_win` | title, evidence, weight, plan?, task? | helpers log liberally; `helper` injected from acting-helper contextvar (`tools/context.py`, same as `save_memory.created_by`) |
| `list_wins` | since?, plan?, weight? | read-only; powers the wins replay |
| `log_checkin` | kind, energy?, notes?, plans_touched? | title auto-generated ("sat jul 12 — morning") |
| `list_checkins` | since?, kind? | |
| `update_plan` extras | why, success_criteria, cadence, status | stewards raise `why` in conversation post-import (V3_DESIGN §5 migration note) |

Persona-card allowlists (already the mechanism) gate them: **mochi gets
read-only wins + check-ins and no task tools** — the ESA never assigns work.

### 4.3 Registration

`build_default_registry()`: the `Config.notion_enabled()` gate becomes a
`WORKSPACE_BACKEND` switch — `"native"` registers `workspace_tools`
unconditionally (no API key needed), `"notion"` keeps today's lazy import
during the transition window (§7). After cutover the branch and the Notion
modules are deleted and workspace tools are simply always on.

---

## 5. The agenda: from cached snapshot to live view

`snapshot_service.py`'s entire reason to exist was Notion latency ("notion
latency never sits in front of the user"). Native queries are local, so the
whole apparatus — `AgendaSnapshot` table, TTL, `is_stale`,
`invalidate_all()`, `ensure_fresh()`, scheduler refresh passes — **deletes**.

`workspace/agenda.py` keeps the consumer-facing surface:

- `get_digest(user_uuid) -> Optional[str]` — now *builds* the digest on
  demand: tasks due today / overdue / in-progress (caps 8/6/5 unchanged),
  active cycle + its `focus`, active plans grouped by helper, wins-this-week
  count, today's window layout (morning/afternoon/evening buckets). This is
  digest **v2** from V3_DESIGN §5 — we get it for free at build time rather
  than as a follow-up.
- `get_payload(user_uuid) -> Optional[dict]` — same bucket keys the
  reconciler reads (`tasks_today`, `tasks_overdue`, `tasks_in_progress`,
  rows shaped like `task_row()`: id/title/status/priority/scheduled/plan/
  cycle/pom + new window/helper). `CompletionReconcilerService._open_tasks`
  keeps working with a one-line import change.
- `ensure_fresh()` / `refresh()` / `invalidate_all()` — deleted; call sites
  in the scheduler and notion tools go with them.

Timezone semantics preserved: "today" = `to_user_timezone(utc_now(), tz)`,
scheduled dates are plain user-local dates, comparison logic ports verbatim.

Token-budget note: digest stays capped (~150–400 tokens) by the same
per-section caps; it lives in the volatile prompt zone, so rendering it live
has zero prompt-cache impact.

---

## 6. One-time import: dainframe → native

`scripts/import_notion_workspace.py` — the last consumer of
`notion/client.py`, folding in V3_DESIGN §5's interactive Projects→Plans
migration (that design survives intact; only the destination changed from
"new Notion dbs" to "native tables"):

1. **`--dry-run`**: pull all Projects, Tasks, Cycles from the dainframe
   (existing `query_all` + `schema.py` readers). Propose Project→Plan
   mappings from `Area` (`Health & Fitness→tempo`, `music→aria`,
   `Writing→poet`, `Code/job search/content creation→pep`,
   `Personal/cooking/Art/Other→chordial`), status mapping
   (`Not started→proposed`, `In progress→active`, `recurring→active` +
   `cadence=loose`, `Done→complete`). Emit a review **yaml** (project →
   helper, status, cadence) and a summary table. No writes.
2. **Dain edits the yaml**, reruns with **`--apply --user <uuid>`**: creates
   plans, then cycles, then tasks (first Project relation → `plan_id`, first
   Sprint relation → `cycle_id`, extras logged; `Scheduled`→`scheduled`,
   `pom estimate`→`pom_estimate`; task status mapped through vocab). Every
   row stores its `notion_page_id`; the run writes an id-map json to
   `backups/` for audit. Idempotent: re-running skips rows whose
   `notion_page_id` already exists — safe to resume.
3. **Not imported, accepted losses:** Notion page *bodies* (no current code
   reads them), edit history, extra relations beyond the first. The Notion
   workspace is left untouched as a frozen archive; nothing deletes it.
4. `Why` / `success_criteria` start blank — steward helpers raise them in
   conversation on first check-in of an inherited plan (unchanged from
   V3_DESIGN).

**Cutover runbook (Dain's instance):** stop writes (just don't chat for a
minute), run importer `--apply`, set `WORKSPACE_BACKEND=native`, restart,
smoke via chat ("what's on today?"), keep `WORKSPACE_BACKEND=notion` as the
one-env-var rollback for a week, then delete the Notion code (§7 phase D).
Goals, wins, and check-ins start empty everywhere — they're new.

---

## 7. Transition mechanics & config

- **`WORKSPACE_BACKEND`** env: `notion` (default at first, current behavior)
  | `native`. Gates tool registration and which agenda implementation backs
  `get_digest/get_payload`. This is a *transition flag with a scheduled
  death*, not a permanent `TaskStore` abstraction — going full bespoke means
  we don't pay for a protocol two implementations will never share again.
- **Config deltas:** add `WORKSPACE_BACKEND`, `WORKSPACE_MAX_PAGE_SIZE`
  (defaults from `NOTION_MAX_PAGE_SIZE`); `AGENDA_ENABLED` survives as the
  digest on/off switch (`agenda_enabled()` no longer requires
  `notion_enabled()`); `NOTION_*` all die in phase D.
- **Prompt-cache note:** tool definitions live in the cached prefix. The
  native tool set changes tool description bytes (and adds tools), so the
  warm cache breaks **once** at the cutover deploy — routine and acceptable
  (same as any tool-set deploy); noted so nobody chases it as a regression.
- **Alembic:** one revision adds the six tables; a second (phase D) drops
  `agenda_snapshots`. Both must be tested against sqlite *and* postgres —
  this lands right around the MULTI_USER_SPEC phase-0 Postgres move, so
  write the migration dialect-clean from the start.

---

## 8. Testing plan

- **`test_workspace_store.py`** — CRUD + invariants: goal/plan consistency,
  `completed_at` stamping, reschedule auto-bump, soft-archive filtering,
  name→id resolution (exact beats substring), public-id parsing.
- **`test_workspace_tools.py`** — replaces `test_notion_tools.py`. Contract
  assertions: the 9 legacy tool names still registered with compatible
  required fields; display-vocab round-trip ("To do" in → `todo` stored →
  "To do" rendered); mutation tools `record_event=True`, list tools False.
- **`test_agenda_native.py`** — replaces `test_agenda_snapshot.py`: bucket
  partition (today/overdue/in-progress) against a fixed user timezone incl.
  the day-boundary cases the old tests cover; digest caps; digest-v2
  sections (focus, wins count, windows).
- **`test_completion_reconciler.py`** — extend, don't rewrite: one new
  end-to-end case where `_open_tasks` reads native payload and the Done mark
  lands in the `tasks` table via the real `update_task` tool.
- **`test_import_notion_workspace.py`** — importer against recorded dainframe
  JSON fixtures (no network): mapping table, idempotent resume, multi-relation
  first-pick logging.
- **Removed:** all mocked-Notion-JSON fixtures once phase D lands.

---

## 9. Stubbed: user interface (explicitly out of scope)

Chat is the only surface this plan ships. Recorded as stubs so the seams are
deliberate:

- **Chat views** — already exist (digest + list tools render promptable
  lines). Digest v2 (§5) is the only "UI" improvement included.
- **`export_workspace`** — *stub*: a `scripts/export_workspace.py` dumping a
  user's plans/goals/tasks/cycles/wins/check-ins to JSON. Cheap insurance
  and the data-portability story; ~half a day; can land any time after
  phase A. Not wired to any user-facing command yet.
- **Web dashboard (read-only)** — *stub*: MULTI_USER_SPEC phase 3. The
  store's query methods (`agenda.get_payload`, `store.list_*`) are designed
  to be the dashboard's read API later; no code reserved for it now.
- **Editable web UI / plan builder** — *stub*: MULTI_USER_SPEC phase 4;
  nothing in this design blocks or anticipates it beyond "all writes go
  through `WorkspaceStore`".

---

## 10. Phasing & estimates

| phase | contents | est. |
|---|---|---|
| **A — schema & store** | models, alembic revision, `vocab.py`, `WorkspaceStore`, store tests | 3–4 days |
| **B — tools & agenda** | `workspace_tools.py` (9 preserved + v3 new), `agenda.py` (digest v2 + payload), reconciler import swap, `WORKSPACE_BACKEND` gate, persona-card allowlists (mochi read-only), tool/agenda/reconciler tests | 4–5 days |
| **C — import & cutover** | importer (dry-run yaml → apply, idempotent), fixtures + tests, Dain's cutover per §6 runbook | 2–3 days |
| **D — burn the boats** | delete `src/services/notion/`, `notion_tools.py`, `AgendaSnapshot` (+drop migration), `NOTION_*` config, smoke script, old tests/docs; write `docs/WORKSPACE.md`; drop the `*_project` tool aliases | 1–2 days, ~a week after cutover |

**Total: ~2–3 weeks**, matching MULTI_USER_SPEC's estimate, with a one-file
rollback lever until phase D.

Sequencing with the v3 launch train: phases A–B are independent of phases
1–3 of V3_DESIGN (they touch disjoint files — same property that let phase 2
run as parallel subagents). The importer's helper assignments *land better
after* the helpers exist and are introduced, so schedule C after the v3
launch train ships; A–B can start immediately.

---

## 11. Open questions (small, none blocking phase A)

1. **Cycle auto-roll:** when `end_date` passes, does the next cycle
   auto-create as `upcoming`→`active`, or does pep's balance pass (future
   work) own that? *Default until then: manual via `create_cycle`, same as
   today.*
2. **Reconciler → wins:** V3_DESIGN wants a cheap pass that logs wins
   noticed in passing. Extend `CompletionReconcilerService`'s existing call
   (it already sees the message + context) rather than adding a fourth
   utility pass? *Lean yes — but it's prompt work; defer to the wins-replay
   feature, the `wins` table and `log_win` tool are ready either way.*
3. **`deprioritized` tasks:** keep excluded from agenda buckets (current
   behavior) but should `list_tasks` grow a `status=deprioritized` filter
   value? *Trivial; default yes.*
