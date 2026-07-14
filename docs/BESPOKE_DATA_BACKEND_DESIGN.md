# Bespoke data backend design

*Status: proposed*  
*Date: 2026-07-12*  
*Companion documents: `MULTI_USER_SPEC.md`, `V3_DESIGN.md`,
`NOTION_INTEGRATION.md`, `DATABASE_MIGRATIONS.md`*

## 1. Decision

Chordial will make its own relational database the authoritative system of
record for plans, goals, tasks, cycles, wins, and check-ins.

Notion will not be required for signup or normal operation. The current
dainframe integration will remain available during migration as a legacy
backend for the existing account. If user demand justifies it, Notion can
later return as an optional projection of Chordial-owned data. Calendar
providers will be optional integrations, not alternate task databases.

This is not simply a Notion-to-SQL port. The native backend will establish a
stable product domain, enforce tenant isolation and lifecycle rules, preserve
change history, and expose one set of use cases to chat tools, scheduled jobs,
a future web UI, and integrations.

### Why this is the right boundary

- Requiring Notion adds OAuth and template provisioning before a user can get
  value from a chat-first companion.
- User-owned Notion schemas make every product change a distributed migration
  over data Chordial cannot control.
- Native reads remove network latency and snapshot staleness from the hot chat
  path.
- Plans, goals, wins, check-ins, helper stewardship, and reschedule behavior
  are product concepts, not Notion concepts. They should be modeled once.
- A first-party database makes permissions, exports, deletion, analytics,
  proactive scheduling, quotas, and future billing tractable.

### Important refinement to the hybrid recommendation

The long-term seam should **not** be a permanently swappable `TaskStore` where
some users store truth in Notion and others in Postgres. That preserves two
sets of semantics and makes every feature support the least capable backend.

Use a temporary `LegacyWorkBackend` router only for migration compatibility.
The permanent design is:

```text
chat tools / scheduler / web UI
              |
        domain services
              |
       native repositories
              |
           Postgres
              |
       outbox + projectors
          /          \
   Notion mirror   calendars
```

Postgres is always authoritative after cutover. Integrations consume changes
and may contribute narrowly defined external facts; they do not implement the
core repository interface.

## 2. Scope

### In scope

- Multi-tenant native storage for plans, goals, tasks, cycles, wins, and
  check-ins.
- Domain services and repositories used by AI tools and background jobs.
- A live agenda query and compact prompt digest.
- Completion reconciliation and automatic win capture.
- Import of the existing dainframe data.
- Export, deletion, audit history, retention, quotas, and operational needs.
- A migration path from SQLite to Postgres.
- Integration boundaries for Google Calendar and an optional Notion mirror.
- Read APIs needed by a small dashboard, even if the dashboard ships later.

### Out of scope for the first backend release

- A full editable task-management web app.
- Collaborative workspaces with multiple human members. “Multi-user” in this
  design means many isolated individual accounts, each with several helpers.
- Arbitrary custom fields, user-defined workflows, subtasks, dependencies,
  Kanban configuration, attachments, and real-time collaborative editing.
- Bidirectional Notion synchronization.
- Bidirectional calendar/task synchronization.
- Billing implementation. The design supplies usage and entitlement seams.

## 3. Product principles encoded in the backend

1. **The user owns the record.** Helpers may propose and steward work, but all
   rows belong to a user and are exportable and deletable.
2. **A planned day is a menu, not a contract.** A task's planned date is not a
   deadline. The schema represents those separately.
3. **Renegotiation is data, not failure.** Moving a planned task records a
   reschedule event and increments a derived count; it does not silently erase
   the old plan.
4. **Accomplishment is durable.** Wins have evidence and provenance and are
   not inferred only from the current state of tasks.
5. **Helpers are actors, not tenants.** `helper_id` attributes stewardship and
   authorship inside a user's tenant; it never replaces `user_uuid` in an
   authorization check.
6. **Local dates are first-class.** “Today,” a planning window, and a deadline
   are user-local calendar concepts. Instants such as an external calendar
   event or an audit timestamp are stored in UTC.
7. **All mutation paths share rules.** Chat tools, reconcilers, imports, and a
   future web UI call the same application services.

## 4. Current-state assessment

The codebase has useful multi-user foundations:

- `User.uuid` is platform-independent.
- `PlatformIdentity` uniquely maps a platform account to a Chordial user.
- Tool handlers receive `user_uuid` from the agent loop rather than from model
  input.
- conversation events, memories, helper state, usage, and traces carry tenant
  attribution.
- Alembic is already the schema authority.

It is not yet safe to call the persistence layer fully multi-tenant:

- several foreign keys and tenant columns are nullable;
- relationships generally lack database-level cascade behavior;
- SQLite foreign-key enforcement is not enabled in `database.py`;
- no repository layer makes tenant predicates mandatory;
- IDs alone identify current Notion pages, and title substring matching can be
  ambiguous;
- the Notion tools accept `user_uuid` but deliberately ignore it;
- `invalidate_all()` marks every user's agenda stale after any Notion write;
- sync SQLAlchemy sessions are opened inside async request/tool paths;
- the agenda cache exists to mask remote reads, not as a native domain view;
- no mutation idempotency key or optimistic concurrency check exists.

The bespoke backend must close these gaps rather than copying the current
patterns blindly.

## 5. Target architecture

### 5.1 Layers

**Domain models** define vocabulary and invariants without provider payloads or
prompt formatting.

**Application services** implement use cases such as `create_task`,
`reschedule_task`, `complete_task`, `build_agenda`, `record_win`, and
`record_check_in`. They own transactions.

**Repositories** perform tenant-scoped persistence. Every method takes a
`user_uuid`; no public `get(id)` method exists.

**Adapters** translate application results for AI tools, the prompt digest,
HTTP endpoints, imports, and external integrations.

**Workers** process the transactional outbox, calendar synchronization,
reconciliation, lifecycle cleanup, and scheduled prompts.

Suggested package shape:

```text
src/
  domain/work/
    enums.py
    entities.py
    commands.py
    errors.py
  repositories/
    work.py                  # protocols
    sqlalchemy_work.py       # implementation
  services/work/
    task_service.py
    plan_service.py
    cycle_service.py
    win_service.py
    checkin_service.py
    agenda_service.py
    import_service.py
  services/tools/
    work_tools.py
  integrations/
    outbox.py
    calendar/
    notion/
      legacy_backend.py
      mirror.py
```

The names may be collapsed while the project is small. The boundaries are
more important than the directory count.

### 5.2 Runtime and database

Use Postgres before opening native storage to untrusted users. SQLite remains
supported for local development and tests during the transition, but public
operation should not depend on a user-count estimate. The switch is driven by
concurrent writes, backup/restore requirements, deploy topology, and the need
for row locking and job claiming.

The current synchronous SQLAlchemy API is acceptable for the local SQLite
phase. Before multiple network-facing workers use Postgres, choose one of:

1. migrate to SQLAlchemy 2 async sessions with `asyncpg` (preferred), or
2. isolate synchronous database calls in a bounded thread pool.

Do not issue blocking Postgres operations directly on the asyncio event loop.

## 6. Data model

### 6.1 Shared conventions

Unless noted otherwise, every domain table has:

| column | type | rule |
|---|---|---|
| `id` | UUID | generated by Chordial; opaque to the model |
| `user_uuid` | UUID/string FK | non-null tenant owner |
| `created_at` | timestamptz | non-null UTC |
| `updated_at` | timestamptz | non-null UTC |
| `archived_at` | timestamptz nullable | soft removal from normal views |
| `version` | integer | starts at 1; increments on mutation |

Existing `users.uuid` is a string. Initial migrations may keep UUID values in
string columns to avoid a risky global key conversion. A later Postgres-only
migration can convert keys to native `uuid` after compatibility tests.

Use explicit scalar columns for queryable product semantics. JSON is reserved
for provider payloads and low-value metadata, not status, dates, or relations.
Use application enums backed by strings plus database check constraints. This
keeps migrations easier than Postgres enum types while still rejecting invalid
values.

All foreign keys use explicit delete behavior. User deletion cascades through
first-party domain data. Historical references within a tenant generally use
`RESTRICT` or are nulled deliberately by a service; database default behavior
must not be accidental.

### 6.2 `plans`

One helper-led arc of work or growth.

| column | type | constraints / meaning |
|---|---|---|
| `title` | text | non-empty, max 300 chars |
| `helper_id` | text | non-null; validated against known persona IDs |
| `status` | text | `proposed`, `active`, `paused`, `complete` |
| `why` | text nullable | preferably the user's words |
| `success_criteria` | text nullable | concrete negotiated finish line |
| `horizon_start` | date nullable | user-local date |
| `horizon_end` | date nullable | must be >= start when both set |
| `cadence` | text | `daily`, `weekly`, `loose` |
| `completed_at` | timestamptz nullable | set iff completed |
| `source` | text | `native`, `notion_import`, `system` |
| `source_ref` | text nullable | import trace only, never authorization |

Indexes: `(user_uuid, status)`, `(user_uuid, helper_id, status)`, and a
case-folded title search index. Titles are not unique; agents and UI must
disambiguate duplicate matches.

### 6.3 `goals`

Milestones inside a plan.

| column | type | constraints / meaning |
|---|---|---|
| `plan_id` | UUID FK | non-null; must reference a plan owned by same user |
| `title` | text | non-empty |
| `status` | text | `not_started`, `in_progress`, `done`, `renegotiated` |
| `target_date` | date nullable | user-local target |
| `done_criteria` | text nullable | concrete criteria |
| `completed_at` | timestamptz nullable | set iff done |
| `sort_order` | integer | stable ordering within a plan |

Tenant safety for relations should be enforced with composite uniqueness and
foreign keys where practical: a parent exposes `UNIQUE(user_uuid, id)` and a
child references `(user_uuid, parent_id)`. This prevents a bug from linking a
row to another tenant even if application filtering fails.

### 6.4 `cycles`

A bounded planning interval that balances work across plans.

| column | type | constraints / meaning |
|---|---|---|
| `title` | text | non-empty |
| `status` | text | `upcoming`, `active`, `complete` |
| `starts_on` | date nullable | user-local |
| `ends_on` | date nullable | >= start |
| `focus` | text nullable | negotiated balance statement |
| `description` | text nullable | supporting context |
| `completed_at` | timestamptz nullable | set iff complete |

Application invariant: at most one active cycle per user. Enforce this in
Postgres with a partial unique index on `user_uuid WHERE status = 'active' AND
archived_at IS NULL`; preserve the same rule in service tests for SQLite.

### 6.5 `tasks`

The executable unit. Tasks may exist without a plan, goal, or cycle for quick
capture.

| column | type | constraints / meaning |
|---|---|---|
| `title` | text | non-empty, max 500 chars |
| `notes` | text nullable | detail not suitable for title |
| `status` | text | `todo`, `in_progress`, `done`, `deprioritized` |
| `priority` | text nullable | `high`, `medium`, `low` |
| `plan_id` | UUID FK nullable | direct plan for goal-less tasks |
| `goal_id` | UUID FK nullable | optional milestone; its plan is authoritative |
| `cycle_id` | UUID FK nullable | optional planning cycle |
| `helper_id` | text nullable | steward for nudging/attribution |
| `planned_for` | date nullable | user-local day the user intends to try it |
| `planning_window` | text nullable | `morning`, `afternoon`, `evening`, `anytime` |
| `due_on` | date nullable | actual deadline; distinct from planned day |
| `estimate_minutes` | integer nullable | positive; replaces pomodoro-specific storage |
| `reschedule_count` | integer | non-negative, service-maintained |
| `completed_at` | timestamptz nullable | set iff done |
| `completion_source` | text nullable | `user`, `assistant`, `reconciler`, `import` |
| `created_by_type` | text | `user`, `helper`, `import`, `system` |
| `created_by_helper_id` | text nullable | attribution when created by helper |
| `source` / `source_ref` | text nullable | import trace |

Rules:

- If `goal_id` is present, `plan_id` is set to that goal's plan in the same
  transaction. The service rejects a conflicting plan.
- Completing a task sets `completed_at`; reopening clears it. Changing status
  is never implemented as a generic unvalidated column patch.
- Changing `planned_for` from one non-null date to another increments
  `reschedule_count` and records an activity event. Initial scheduling and
  clearing a date have separately named activities.
- “Overdue” means `due_on < today` and not done. A task merely planned on a
  previous day is “carried over,” not overdue.
- `estimate_minutes` is the canonical value. Imports convert one pomodoro to a
  configured number of minutes and preserve the original in import metadata.

Indexes:

- `(user_uuid, status, planned_for)`
- `(user_uuid, status, due_on)`
- `(user_uuid, cycle_id, status)`
- `(user_uuid, plan_id, status)`
- `(user_uuid, helper_id, status)`
- partial index for open tasks (`status IN ('todo', 'in_progress')`)

### 6.6 `wins`

An immutable-leaning record of accomplishment. Correction is allowed, but a
win should not disappear just because a linked task or plan later changes.

| column | type | constraints / meaning |
|---|---|---|
| `title` | text | concrete, preferably past tense |
| `occurred_on` | date | user-local date |
| `helper_id` | text nullable | witness/logger |
| `plan_id` | UUID FK nullable | `ON DELETE SET NULL` for durable history |
| `task_id` | UUID FK nullable | `ON DELETE SET NULL` |
| `evidence` | text nullable | user's words; sensitive content |
| `weight` | text | `spark`, `solid`, `milestone` |
| `source` | text | `user`, `assistant`, `reconciler`, `task_completion`, `import` |
| `dedupe_key` | text nullable | unique per user when supplied |

Automatic win creation should be conservative and idempotent. Task completion
does not have to create a win for every trivial task; the service can use an
explicit `record_as_win` decision. When it does, use a dedupe key such as
`task-completion:<task-id>:<completion-version>`.

### 6.7 `check_ins` and `check_in_plans`

`check_ins` stores the daily shared journal:

| column | type | constraints / meaning |
|---|---|---|
| `occurred_on` | date | user-local date |
| `kind` | text | `morning`, `evening`, `adhoc` |
| `energy` | text nullable | `low`, `ok`, `good`, `great` |
| `notes` | text nullable | what the user shared |
| `helper_id` | text nullable | helper who led/logged it |
| `source_event_id` | integer nullable | originating conversation event |

`check_in_plans(user_uuid, check_in_id, plan_id)` is the many-to-many join and
uses composite tenant-safe foreign keys. Do not store plan IDs as JSON.

Duplicate automated morning/evening rows are prevented by a unique constraint
on `(user_uuid, occurred_on, kind)` for those kinds. Ad-hoc rows remain
unrestricted; implement this as a Postgres partial unique index and service
invariant in SQLite.

### 6.8 `work_activity`

An append-only audit and product history stream for meaningful domain changes.
It is not a full event-sourced database; current-state tables remain canonical.

| column | type | meaning |
|---|---|---|
| `id` | bigint | ordered event ID |
| `user_uuid` | tenant FK | non-null |
| `entity_type` | text | plan, goal, task, cycle, win, check-in |
| `entity_id` | UUID | affected row |
| `action` | text | created, completed, rescheduled, reopened, etc. |
| `actor_type` | text | user, helper, reconciler, import, system |
| `actor_id` | text nullable | helper or platform identity reference |
| `source_event_id` | integer nullable | conversation provenance |
| `before` | JSON nullable | allowlisted changed fields only |
| `after` | JSON nullable | allowlisted changed fields only |
| `created_at` | timestamptz | UTC |

Do not copy entire notes or win evidence into activity JSON. History should not
double the footprint of sensitive text or complicate deletion.

This table supports reschedule history, debugging, the dashboard activity
feed, integration projections, and later analytics.

### 6.9 Idempotency and outbox

`mutation_requests` prevents duplicate side effects when a platform retries an
update or an agent/tool turn is replayed:

| column | meaning |
|---|---|
| `user_uuid`, `idempotency_key` | unique pair |
| `operation` | logical command name |
| `result` | small JSON response |
| `created_at`, `expires_at` | retention |

The agent service should eventually pass the tool call ID plus conversation
event/turn identity as the idempotency key. Imports and web endpoints provide
their own stable keys.

`outbox_events` is written in the same transaction as a domain mutation. A
worker claims pending rows with `FOR UPDATE SKIP LOCKED`, delivers to a
projector/integration, and records attempts and completion. Fields include
`topic`, `aggregate_id`, `payload`, `available_at`, `attempts`, `locked_at`,
`processed_at`, and `last_error`.

The outbox is required before any external mirror is enabled; otherwise a DB
commit followed by process failure can permanently miss a sync.

### 6.10 Integration tables

Use generic connection and link primitives, with encrypted secrets separated
from domain rows:

- `integration_connections`: user, provider, status, provider account ID,
  scopes, encrypted credential envelope, timestamps, last error.
- `external_links`: user, provider, entity type/id, external object ID,
  external revision, last pushed/pulled timestamps, sync status.
- `sync_cursors`: connection, resource kind, provider cursor/token.
- `oauth_states`: short-lived, hashed state/PKCE data for callback validation.

Credentials must be encrypted with an application-level envelope key managed
outside the database. Never put access/refresh tokens in logs, activity JSON,
or exports.

## 7. Tenant isolation and authorization

Tenant isolation is a launch invariant, not merely a convention.

### Required controls

1. Every repository method requires `user_uuid` and filters by it.
2. Every child-to-parent lookup verifies both parent ID and tenant.
3. Tool and HTTP inputs never contain a selectable `user_uuid`; identity is
   injected from authenticated context.
4. Cross-tenant IDs return `not found`, not `forbidden`, to avoid existence
   disclosure.
5. Composite tenant foreign keys prevent cross-tenant relations.
6. Automated tests create at least two users and attempt every read/mutation
   with the other user's IDs.
7. Admin/maintenance access uses distinct methods and credentials; it is not
   a boolean bypass in normal repositories.

Before a public web API or multiple internal services are exposed, add
Postgres row-level security as defense in depth. Set a transaction-local
`app.user_uuid` and define policies against it. Background jobs that process
many tenants use a separate restricted worker role and still call tenant-
scoped services. RLS does not replace repository tests.

## 8. Domain service contracts

Representative commands:

```python
create_task(ctx, title, plan_id=None, goal_id=None, cycle_id=None,
            planned_for=None, due_on=None, planning_window=None,
            priority=None, estimate_minutes=None, idempotency_key=None)

update_task_details(ctx, task_id, expected_version, title=UNSET,
                    notes=UNSET, priority=UNSET, estimate_minutes=UNSET)

schedule_task(ctx, task_id, planned_for, planning_window=None,
              expected_version=None)

complete_task(ctx, task_id, evidence=None, record_as_win=False,
              expected_version=None, idempotency_key=None)

list_tasks(ctx, filters, cursor=None, limit=25)
build_agenda(ctx, local_date)
```

`ctx` carries authenticated `user_uuid`, actor type/ID, optional conversation
event ID, and request ID. It is created outside the model-facing tool schema.

Mutations run in one transaction:

1. claim/check idempotency key;
2. load the tenant-scoped row, optionally checking `version`;
3. validate transition and related entities;
4. write current state;
5. append `work_activity`;
6. enqueue outbox events;
7. save the stable result for replay;
8. commit.

Use typed domain errors (`NotFound`, `AmbiguousMatch`, `InvalidTransition`,
`Conflict`, `QuotaExceeded`) and translate them at adapters. Do not return
human prose from repositories.

### Title resolution for AI tools

The current Notion implementation resolves exact titles and then accepts the
first case-insensitive substring. That can mutate the wrong row.

Native tools should:

- prefer opaque IDs returned in recent listings;
- accept exact case-insensitive title only when it resolves to one open row;
- return a short disambiguation list when multiple rows match;
- use fuzzy/substring search for suggestions, never silent mutation targeting.

## 9. AI tool surface

Keep model-facing operations small and semantic. Initial tools:

- `list_tasks`, `create_task`, `update_task`, `complete_task`
- `list_plans`, `create_plan`, `update_plan`
- `list_goals`, `create_goal`, `update_goal`
- `list_cycles`, `create_cycle`, `update_cycle`
- `list_wins`, `record_win`
- `record_check_in`

Avoid one generic CRUD tool. Named operations make transition rules and
persona allowlists legible. In particular, scheduling, completion, reopening,
and deprioritization should be explicit operations even if the public tool
count is initially consolidated behind `update_task`.

Tool responses remain compact, stable strings for the agent loop, but are
rendered from typed service results. Pure reads keep `record_event=False`;
successful mutations remain recorded to avoid cross-turn repetition.

Update persona cards deliberately:

- Chordial: full general work surface.
- Tempo/Aria/Poet/Pep: relevant plan, goal, task, win, and check-in operations.
- Mochi: wins and check-ins; no task assignment by default.
- Curator: no direct work mutations.

## 10. Agenda and prompt context

Native storage removes the need for a TTL-based Notion snapshot. Replace
`AgendaSnapshotService` with `AgendaService`:

1. determine the user's local date from `User.timezone`;
2. query active cycle, active plans, open tasks for the day, carried-over
   planned tasks, true overdue tasks, and in-progress tasks;
3. query recent wins and relevant check-ins as needed by the scheduled flow;
4. render a bounded digest for the volatile prompt zone.

Suggested payload:

```json
{
  "local_date": "2026-07-12",
  "active_cycle": {},
  "tasks_by_window": {
    "morning": [], "afternoon": [], "evening": [], "anytime": []
  },
  "carried_over": [],
  "overdue": [],
  "in_progress": [],
  "active_plans_by_helper": {},
  "wins_today": [],
  "wins_this_week_count": 0
}
```

The query should have a fixed upper bound and select only necessary columns.
Keep prompt caps similar to today's digest so native abundance does not bloat
model input.

At small scale, build the digest on demand from indexed local queries; this is
normally faster and more correct than cache invalidation. If profiling later
shows digest rendering is material, cache per user/date with a monotonically
increasing `work_version` on the user/account—not TTL and not global
invalidation.

Compatibility during migration:

- `NativeAgendaService` reads native tables.
- `LegacyNotionAgendaService` is today's snapshot service.
- `AgendaServiceRouter` chooses by a temporary user migration state.
- delete the router and legacy snapshot table after the dainframe cutover is
  accepted.

Rename prompt copy from “notion agenda” to “agenda.”

## 11. Completion reconciliation and wins

Preserve the current narrow utility-model pass and its strongest safety rule:
the model may only select from server-supplied open task IDs.

New flow:

1. after a user message and helper response, query a bounded candidate set of
   tenant-owned open tasks;
2. ask the utility model for completed task IDs and optional evidence spans;
3. validate IDs against the candidate set again;
4. call `complete_task` with an idempotency key derived from user message ID
   and task ID;
5. optionally create a win in the same transaction or enqueue a separate
   accomplishment classifier;
6. do not let reconciliation failure affect the user-visible reply.

The native reconciler must not call the model-facing `ToolRegistry` to perform
its write. It should call the same domain service directly with actor type
`reconciler`. This removes prose parsing and makes transaction/idempotency
behavior explicit.

Capture evidence conservatively. Store a short user-authored excerpt or a
paraphrase with source provenance; do not copy entire sensitive messages into
every win.

## 12. Calendar integration design

Calendar is not a replacement for the native task model. It contributes time
constraints and receives selected scheduled commitments.

### V1: read-only availability

- OAuth connection per user.
- Request the minimum read-only calendar scope.
- Fetch free/busy for a bounded horizon, preferably on demand with a short
  cache.
- Inject only availability summaries into planning; do not put raw event
  titles into prompts unless the user explicitly enables that privacy level.
- Store provider event metadata only as needed for caching and cursor sync.

### V2: explicit event creation

- A user or helper may convert a task to a calendar block through an explicit
  command.
- Chordial creates the event and records an `external_link`.
- Task title/date changes do not silently rewrite the calendar until the user
  opts into that behavior.
- Deleting/completing a task does not delete an event without confirmation.

### Conflict policy

Native task state wins for task fields; calendar state wins for external event
attendance and times. If a linked event moves externally, record the new
calendar time and surface a proposed task reschedule rather than overwriting
`planned_for` silently.

## 13. Optional Notion integration

### Migration bridge

Keep the current single-workspace client only for the existing account while
native import and parity are validated. Hide it behind
`LegacyWorkBackend`/`LegacyNotionAgendaService` and a user-level migration
state (`legacy_notion`, `dual_read_compare`, `native`). Do not generalize its
global token or schema assumptions for new users.

### Future mirror, only if demanded

- Public Notion OAuth connection per user.
- Native database remains authoritative.
- Provision a Chordial-owned template/schema and store stable property IDs.
- Project native mutations through the transactional outbox.
- Begin with one-way Chordial → Notion sync.
- Mark mirrored pages with Chordial entity IDs.
- Treat user edits in the mirror as unsupported or surface them as conflicts;
  never accept them silently into native truth.
- Provide “rebuild mirror” as recovery from schema damage.

This still carries integration cost, but it is optional and failure cannot
block chat or lose canonical data.

## 14. Import and cutover plan

### 14.1 Import mapping

| current Notion | native |
|---|---|
| Project | Plan |
| Project Area | helper assignment proposal and import metadata |
| Project Status | mapped plan status |
| Project description | plan supporting text; do not invent `why` |
| Task Project relation | task `plan_id` |
| Task Sprint relation | task `cycle_id` |
| Task Scheduled | initial `planned_for` (not `due_on`) |
| Task pom estimate | `estimate_minutes` using configured conversion |
| Cycle goal | cycle `focus` or description, human-reviewed |

Goals, wins, and check-ins begin empty unless separately derived. Do not use an
LLM to fabricate them during migration.

### 14.2 Import properties

- Dry-run by default; emit counts, mappings, unresolved relations, invalid
  dates/options, and proposed helper assignments.
- Use stable idempotency keys based on Notion page IDs.
- Preserve source page ID and last-edited timestamp for traceability.
- Import parents before children.
- Batch commits so a failure can resume without duplicating rows.
- Compare counts and sampled rendered records after import.
- Never write back to Notion during import.

### 14.3 Cutover states

1. **Legacy:** current Notion tools and snapshots remain live.
2. **Import preview:** repeatable dry run and human-reviewed mappings.
3. **Native shadow:** import, then compare native and Notion agenda payloads;
   Notion still handles mutations.
4. **Brief write freeze:** stop Notion mutation tools, run delta import from
   last-edited timestamps, validate.
5. **Native:** switch tool registry and agenda service; keep Notion read-only
   for rollback observation.
6. **Acceptance window:** verify daily use, counts, relations, and completion
   behavior for at least one full cycle.
7. **Retire legacy:** archive credentials/code path only after explicit signoff.

Avoid indefinite dual-write. Without a mature outbox and conflict resolver it
creates two truths and a harder recovery problem than a short controlled
freeze.

### 14.4 Rollback

During the acceptance window, rollback means returning reads/tools to Notion.
Native-only changes made after cutover must be exported as a reviewed patch;
do not automatically replay them into Notion. State the rollback window and
data-handling rule before cutover.

## 15. Existing database migration plan

Recommended Alembic sequence:

1. harden existing user foreign keys, defaults, indexes, and SQLite FK pragma;
2. add native domain tables and constraints without switching behavior;
3. add activity, idempotency, outbox, and integration tables;
4. add per-user backend migration state and account-level work version;
5. deploy code capable of old and new reads;
6. run dainframe import/cutover;
7. remove agenda snapshot and legacy routing in a later release;
8. separately migrate production data from SQLite to Postgres, with source
   quiescence, checksums/counts, and restore rehearsal.

Do not combine the dainframe semantic migration and SQLite→Postgres engine
migration in the same cutover. Separating them keeps rollback understandable.

## 16. API surface for a read-only dashboard

The domain services should support these authenticated endpoints even if the
first UI is server-rendered:

```text
GET /api/v1/agenda?date=YYYY-MM-DD
GET /api/v1/tasks?status=&plan_id=&cursor=&limit=
GET /api/v1/plans?status=&cursor=&limit=
GET /api/v1/plans/{id}
GET /api/v1/cycles/current
GET /api/v1/wins?from=&to=&cursor=&limit=
GET /api/v1/check-ins?from=&to=&cursor=&limit=
GET /api/v1/export
DELETE /api/v1/account
```

Use cursor pagination, maximum page sizes, and typed response schemas. Mutation
endpoints later require `Idempotency-Key` and `If-Match`/version semantics.
Do not expose ORM models directly.

Web authentication can begin with Telegram login verification mapped to
`PlatformIdentity`, but the backend should model an authenticated session
separately: hashed rotating session token, expiry, revocation, CSRF protection
for cookie auth, and recent-auth confirmation for export/deletion/integration
changes. Email magic links can be added without changing domain ownership.

## 17. Privacy, retention, export, and deletion

### Privacy

- Encrypt transport and database backups.
- Encrypt integration credentials at application level.
- Restrict production database and support access; log audited admin access.
- Redact task notes, check-in notes, win evidence, OAuth data, and message text
  from structured logs and errors.
- Treat check-ins and evidence as sensitive personal data.
- Minimize raw calendar detail in storage and model prompts.

### Export

Produce a versioned ZIP containing JSON (canonical), plus friendly CSVs for
plans, goals, tasks, cycles, wins, and check-ins. Include relations by stable
ID, timestamps, status history/activity, timezone, and schema version. Exclude
secrets and internal model traces unless separately requested by policy.

### Deletion

Use a confirmed, asynchronous account-deletion workflow:

1. immediately deactivate the account and revoke sessions;
2. revoke external provider credentials;
3. enqueue deletion of provider mirrors where policy and user choice require;
4. cascade-delete first-party user content in bounded transactions;
5. tombstone billing/legal records only where retention is required;
6. expire backups according to documented backup retention;
7. record a non-identifying deletion receipt.

Define this policy before public signup; schema cascades alone are not a full
account deletion process.

## 18. Quotas and abuse controls

Native task writes are cheap, but public users can still create unbounded
storage and trigger AI spend. Add entitlements before public signup:

- active task/plan limits by tier;
- write rate limits per user and platform identity;
- maximum text lengths and pagination limits;
- per-user daily/monthly model spend allowance from `UsageLog`;
- proactive generation disabled first as allowance is approached;
- hard cap before expensive chat generation when exhausted;
- separate system/admin override with audit trail.

Keep quota decisions in a service, not scattered through tools. Store current
plan/entitlement assignments in first-party tables; do not infer them from a
payment provider on every request.

## 19. Reliability and operational model

### Transactions and concurrency

- Mutations are short database transactions; never hold one open across an AI
  or provider call.
- Use optimistic concurrency (`version`) for web edits and background jobs.
- Use row locks only for narrow transitions such as activating one cycle or
  claiming an outbox job.
- Retry serialization/deadlock failures with bounded jitter.
- Set database statement and connection acquisition timeouts.

### Backups

For Postgres: automated encrypted backups plus point-in-time recovery, with a
documented recovery point objective and recovery time objective. A backup is
not trusted until a restore into an isolated environment is rehearsed.

Suggested initial targets:

- RPO: <= 15 minutes
- RTO: <= 4 hours
- monthly restore test, increasing frequency before major migrations

### Jobs

Postgres-backed job claiming is enough initially. Separate workers are useful
for scheduler, reconciliation, curation, and integrations once load or failure
isolation demands it. Redis/Celery is not a prerequisite.

The current scheduler loops through users sequentially. Before large rollout,
bound concurrency and make work claimable/idempotent so one slow user or AI
call cannot delay the entire population.

## 20. Observability

Emit structured, privacy-safe telemetry with request/turn ID, hashed or
internal user ID, helper ID, operation, latency, result category, and database
query count. Never log content fields by default.

Initial metrics:

- domain command success/error/conflict counts and p50/p95/p99 latency;
- agenda query latency and digest size;
- open tasks/plans per active user (aggregated);
- reconciliation candidates, matches, rejected IDs, and failures;
- outbox lag, attempts, dead letters, and integration error rate;
- database connections, slow queries, lock waits, storage, and replication
  lag;
- per-user and aggregate AI cost/quota denial rates;
- import counts, unresolved relations, and comparison mismatches.

Alert on sustained error rate, outbox lag, backup failure, quota enforcement
failure, scheduler lag, database saturation, and cross-tenant test canary
failure.

## 21. Test strategy

### Unit tests

- every state transition and invalid transition;
- planned date versus due date semantics;
- automatic reschedule history/count;
- task/goal/plan relation consistency;
- timezone boundaries and daylight-saving transitions;
- title ambiguity behavior;
- digest caps and deterministic rendering;
- win deduplication and completion idempotency.

### Repository contract tests

Run the same suite against SQLite and Postgres during transition. Test tenant
filters, composite foreign keys, cascades, unique constraints, optimistic
locking, transaction rollback, cursor pagination, and job claiming.

### Tenant isolation suite

For each repository, service, tool, and API operation:

1. create users A and B with overlapping titles;
2. pass B's opaque IDs while authenticated as A;
3. assert no read, mutation, relation, error detail, activity, or outbox event
   leaks across the boundary.

### Migration tests

- fixture exports representing missing properties, renamed options, dangling
  relations, duplicate titles, and invalid dates;
- repeat import twice and prove no duplicates;
- compare legacy and native agenda fixtures;
- dry-run has zero writes;
- cutover and rollback rehearsed on a production-shaped copy.

### End-to-end tests

- chat capture → tool mutation → agenda appears next turn;
- completion mention → reconciler completes only valid task → optional win;
- reschedule → history/count → renegotiation signal;
- morning/evening check-in dedupe;
- account export and deletion;
- provider outage does not block native chat/task operation.

## 22. Delivery plan and estimates

Estimates assume one engineer familiar with the codebase and include tests and
review, not a polished editable dashboard.

### Phase A — persistence foundation (1–2 weeks)

- choose Postgres deployment and async/session strategy;
- add domain tables, constraints, indexes, activity, idempotency, and outbox;
- repository contracts and tenant-isolation tests;
- backup and restore baseline.

Exit: two users can safely exercise repositories without cross-tenant access;
migrations pass on SQLite and Postgres.

### Phase B — native use cases and tools (1.5–2.5 weeks)

- domain services and typed errors;
- native tools and persona allowlists;
- native agenda query/digest;
- reconciler direct service integration and win capture;
- scheduler integration.

Exit: all current task/project/cycle chat behaviors work natively, plus the v3
plan/goal/win/check-in primitives.

### Phase C — dainframe import and cutover (0.5–1.5 weeks)

- dry-run importer and mapping review;
- native shadow comparisons;
- controlled cutover and acceptance window;
- rollback/export tooling.

Exit: existing data is reconciled and daily operation no longer requires
Notion.

### Phase D — public-user hardening (1.5–3 weeks)

- signup/invite gating, sessions if a web surface exists, quotas;
- export/delete lifecycle and privacy controls;
- scheduler concurrency, observability, alerts, rate limits;
- deployment and recovery rehearsal.

Exit: invited strangers can use native storage with bounded cost and an
operable recovery story.

### Phase E — optional visibility/integrations (separate)

- read-only dashboard: about 1–2 weeks for a deliberately small surface;
- calendar free/busy and OAuth: about 1–2 weeks;
- explicit calendar event creation: about 1 additional week;
- one-way Notion mirror: 2–4 weeks only after demand is demonstrated.

The original 2–3 week estimate is plausible for a local feature-parity
prototype. An implementation that is genuinely ready for multiple untrusted
users is closer to **4–7 weeks for the backend and hardening**, and roughly
**6–10 weeks** including migration and a small read-only surface. Calendar and
Notion mirror work should not sit on the critical path.

## 23. Acceptance criteria

The bespoke backend is ready for invited multi-user use when:

- Notion is not required to create an account, create work, build an agenda,
  complete a task, or record a win/check-in.
- Every domain access path is tenant-scoped and the cross-tenant suite passes.
- Native state changes, activity, idempotency records, and outbox entries are
  transactionally consistent.
- Agenda reads are fresh without background Notion refreshes or global cache
  invalidation.
- The reconciler can only complete supplied, open, tenant-owned task IDs.
- Duplicate deliveries/tool calls do not duplicate tasks, completions, wins,
  or check-ins.
- Existing dainframe records have a reviewed import report and rollback plan.
- Per-user model/storage/rate quotas are enforced before expensive work.
- Export and account deletion work end to end.
- Postgres backups and restore have been exercised.
- Metrics expose scheduler lag, DB health, outbox lag, errors, and cost.
- A Notion or calendar outage cannot prevent native work operations.

## 24. Decisions to lock before implementation

These are bounded product decisions; none changes the recommendation:

1. Does a normal task completion create a win only when explicitly requested,
   or may the reconciler classify noteworthy completions automatically?
   Recommended: explicit plus conservative automatic classification.
2. What is the default pomodoro-to-minute conversion for import?
   Recommended: configurable, default 25 minutes.
3. How long is the legacy rollback/acceptance window?
   Recommended: one complete cycle, at least 14 days.
4. Is free/busy calendar access part of first public launch?
   Recommended: no; keep it a fast-follow.
5. Is the first dashboard read-only?
   Recommended: yes. Validate glanceability before building another full task
   editor.

## 25. Final recommendation

Proceed with the native backend, but budget it as a product and tenancy
foundation rather than a mechanical Notion replacement. Build Postgres-backed
domain services first; keep the present Notion path only as a short-lived
migration adapter; cut over the dainframe with a controlled import; and add
calendar or Notion projections after the native workflow proves itself.

This gives Chordial one evolvable model, one authorization boundary, fresh
agenda awareness, and a path from a personal companion to a multi-user service
without making every future feature negotiate with a third-party schema.
