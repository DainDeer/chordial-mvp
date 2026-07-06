# Notion integration

Chordial can read and write the user's Notion workspace (**the dainframe**)
through tool calls in the agentic loop. This document describes how the
integration is built and how the model actually reaches for it during an
ordinary conversation.

## Overview

The dainframe is a Linear-style GTD setup with three linked databases:

- **tasks** — the day-to-day to-dos (title, status, priority, a relation to a
  project, a relation to a sprint/cycle, a scheduled date, a pomodoro estimate)
- **projects** — larger efforts (title, status, one or more area tags, a
  description)
- **cycles** — sprints (title, status, a date range, a goal, a description)

Tasks relate to projects (`Project`) and to cycles (`Sprint`). The integration
encodes this schema once and exposes nine domain-specific tools so the model
works in the user's own vocabulary — status names, priorities, project and
sprint names — never raw Notion property JSON or page IDs.

## Architecture

The integration slots into the existing agent loop with no changes to
`AgentService` or the provider layer. A tool is a `ToolDef` (what the model
sees) paired with an async handler (what runs); the loop injects `user_uuid`
and feeds handler output back to the model. Registering a capability is a
`register()` call — nothing else moves.

```
src/
├── services/
│   ├── notion/
│   │   ├── __init__.py        # exports NotionClient, NotionError, get_client
│   │   ├── client.py          # async httpx wrapper over the Notion REST API
│   │   └── schema.py          # the dainframe schema: enums, property/filter
│   │                          #   builders, page formatters
│   └── tools/
│       ├── notion_tools.py    # the 9 model-facing tools + handlers
│       └── __init__.py        # registers Notion tools iff NOTION_API_KEY set
config.py                      # NOTION_API_KEY, DB IDs, version, page-size cap
scripts/notion_smoke_test.py   # live read/write check against the real dainframe
tests/test_notion_tools.py     # unit tests with a fake client (no network)
```

**`client.py`** keeps a deliberately small surface: `query_database` /
`query_all` (with pagination), `create_page`, `update_page`, `retrieve_page`.
One shared `httpx.AsyncClient` is created lazily and reused, because handlers
run concurrently under `asyncio.gather` in the loop and a shared connection
pool is the right shape for that. Non-2xx responses become `NotionError`
carrying Notion's own message and error code; the loop already catches
exceptions and returns them to the model as readable text, so a bad enum or a
missing page produces a recoverable message rather than a crashed turn.

**`schema.py`** is the single source of truth for the dainframe's shape. It
holds the controlled vocabularies (`TASK_STATUS`, `TASK_PRIORITY`,
`PROJECT_STATUS`, `PROJECT_AREA`, `CYCLE_STATUS`), the builders that turn
friendly arguments into Notion property payloads (title, rich_text, select,
status, multi_select, number, date, relation), the filter builders, and the
formatters that turn a page back into a short, promptable one-liner. If the
dainframe schema changes, this is the one file to edit.

**`notion_tools.py`** holds the handlers. They accept names, not IDs: a handler
resolves a project or sprint name to a page ID by querying the relevant
database (exact title first, then a case-insensitive substring match), and
returns a clear message if nothing matches. Updates identify their target by
title *or* ID — a 32-hex/UUID string is used directly, anything else is
resolved by title.

**Configuration** lives in `config.py`. Only `NOTION_API_KEY` is required; the
three database IDs default to the dainframe and can be overridden by env. When
the key is absent the tools are simply not registered and the bot runs exactly
as before. Setup steps for the token are in `NOTION_SETUP.md`.

## Tool catalog

| Tool | Direction | Key arguments |
| --- | --- | --- |
| `list_tasks` | read | `status`, `priority`, `project`, `sprint`, `scheduled_on_or_after`, `scheduled_on_or_before`, `limit` |
| `create_task` | write | `title` (required), `status`, `priority`, `project`, `sprint`, `scheduled_date`, `pom_estimate` |
| `update_task` | write | `task` (title or id, required), `new_title`, `status`, `priority`, `project`, `sprint`, `scheduled_date`, `pom_estimate` |
| `list_projects` | read | `status`, `area`, `limit` |
| `create_project` | write | `title` (required), `status`, `area[]`, `description` |
| `update_project` | write | `project` (title or id, required), `new_title`, `status`, `area[]`, `description` |
| `list_cycles` | read | `status`, `limit` |
| `create_cycle` | write | `title` (required), `status`, `start_date`, `end_date`, `goal`, `description` |
| `update_cycle` | write | `cycle` (title or id, required), `new_title`, `status`, `start_date`, `end_date`, `goal`, `description` |

Enum-typed arguments (`status`, `priority`, `area`) are constrained in each
tool's JSON Schema to the exact dainframe values, so the model chooses from a
menu and cannot invent an unknown status. Defaults mirror how a person would
expect capture to behave: a new task is `To do`, a new project is
`Not started`, a new cycle is `Upcoming`. Every `list_*` line ends with the
page `id`, which gives the model an unambiguous handle for a follow-up update
in the same turn.

## How an agent uses this from chat

The user never mentions tools, IDs, or Notion's data model. They talk about
their day; the model maps that to a tool call, reads the result, and replies in
natural language. Below, each example shows the user's message and the tool
call the model makes.

**Reading — "what's on my plate?"**

> **User:** what am I supposed to be doing this week?

```json
{ "tool": "list_tasks",
  "input": { "scheduled_on_or_after": "2026-07-06",
             "scheduled_on_or_before": "2026-07-12" } }
```

The handler returns a short list; the model summarizes it warmly rather than
dumping rows.

**Capture — "remind me to…"**

> **User:** i need to refill my adhd meds, that's important

```json
{ "tool": "create_task",
  "input": { "title": "refill adhd meds — schedule appt", "priority": "high" } }
```

**Linking by name — no IDs anywhere**

> **User:** add "wire up the calendar view" to the Fallow Deer sprint

```json
{ "tool": "create_task",
  "input": { "title": "wire up the calendar view", "sprint": "Fallow Deer" } }
```

The handler resolves `"Fallow Deer"` to its cycle page and sets the relation.
If no such sprint exists, it says so and the model can offer to create one.

**Completion / status change**

> **User:** okay I finished the abs workout

```json
{ "tool": "update_task", "input": { "task": "abs", "status": "Done" } }
```

`update_task` finds the task by title, so the model doesn't need to have listed
it first.

**Multi-step in one turn**

> **User:** what's in progress right now, and can you bump the piano one to high priority?

The model calls `list_tasks` with `{"status": "In progress"}`, reads the line
`… "Piano practice" … id=abc123 …`, and immediately follows with
`update_task` using either the title or the returned id. The loop runs both
before replying.

**Planning a sprint**

> **User:** start a new sprint called Roe Deer for the next two weeks, goal is to ship the notion integration

```json
{ "tool": "create_cycle",
  "input": { "title": "Roe Deer", "start_date": "2026-07-06",
             "end_date": "2026-07-20", "status": "Active",
             "goal": "ship the notion integration" } }
```

Because relations resolve by name and the schema is baked into the tool
definitions, the model can carry out "add this to that sprint", "what's due
Friday", or "mark it done" from ordinary conversation, and the user never sees
the machinery underneath.

## Failure modes

- **Unknown name** (project/sprint that doesn't exist) → the handler returns a
  message naming what it couldn't find, so the model can ask or offer to create
  it.
- **Bad enum / Notion validation error** → surfaced as `NotionError` text; the
  loop feeds it back and the model retries or explains.
- **No matching task/project/cycle on update** → a "no … matching '…'" message
  rather than a silent no-op.
- **Empty update** (an update call with no changeable fields) → an explicit
  "nothing to update" message.

## Verification

`tests/test_notion_tools.py` runs the handlers against a fake client (no
network, no key required) and asserts that payloads match the dainframe schema,
that names resolve to relations, and that list output is formatted as expected.
For a live check against the real workspace, run `scripts/notion_smoke_test.py`
(add `--write` to also exercise create/update).

## Notes and future work

- The REST API version is pinned to `2022-06-28`, which queries by database ID
  and works with the dainframe's single-source databases. Splitting a database
  into multiple data sources later would require the newer data-source
  endpoints.
- `httpx` was added to `pyproject.toml`; run `poetry lock && poetry install`
  after pulling.
- Natural extensions: appending page content/body blocks (not just properties),
  a generic escape-hatch query tool, and reading task comments.
