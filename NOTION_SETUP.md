# Notion integration (the dainframe)

Chordial can read and write your Notion tasks, projects, and cycles through
tool calls. It talks to the Notion REST API directly using an **internal
integration token** — the bot never sees your Notion password, and you can
revoke access at any time.

## 1. Create an internal integration

1. Go to <https://www.notion.so/my-integrations> and click **New integration**.
2. Name it (e.g. `chordial`), pick the workspace that holds **the dainframe**,
   and set the capabilities to **Read content**, **Update content**, and
   **Insert content**. (No user information needed.)
3. Click **Submit**, then copy the **Internal Integration Secret**. It starts
   with `ntn_` (older tokens start with `secret_`).

## 2. Share the dainframe with the integration

An integration only sees pages that have been shared with it. The three
databases live under the **the dainframe** page, so sharing that one page
cascades to all of them:

1. Open **the dainframe** page in Notion.
2. Click the **•••** menu (top-right) → **Connections** → **Connect to** →
   select your `chordial` integration.
3. Confirm. The tasks, projects, and cycles databases inherit the connection.

## 3. Add the token to `.env`

```dotenv
NOTION_API_KEY=ntn_your_secret_here
```

That's the only required variable. The database IDs are already defaulted to
the dainframe in `config.py`; override them only if you point the integration
at a different workspace:

```dotenv
# optional overrides
NOTION_TASKS_DB_ID=9d5b5399-f284-481b-8d2a-e4797c6db18a
NOTION_PROJECTS_DB_ID=0af777e5-3988-4a65-b9a0-1672524d9952
NOTION_CYCLES_DB_ID=c21c7869-4672-4bf1-8cd1-d5af73282572
NOTION_API_VERSION=2022-06-28
NOTION_MAX_PAGE_SIZE=25
```

When `NOTION_API_KEY` is set, Chordial registers the Notion tools on startup
(look for `notion tools enabled (9 registered)` in the logs). When it's unset,
the bot runs exactly as before with those tools simply absent.

## 4. What the bot can do

Nine domain-specific tools, all scoped to the dainframe schema so the model
picks from your real status/priority/area values instead of guessing:

| Tool | What it does |
| --- | --- |
| `list_tasks` | Filter tasks by status, priority, project, sprint, or scheduled-date range |
| `create_task` | Add a task (links to a project/sprint by name) |
| `update_task` | Mark done, reprioritize, reschedule, re-link, or rename a task |
| `list_projects` | Filter projects by status or area |
| `create_project` / `update_project` | Manage projects |
| `list_cycles` | Find cycles/sprints (e.g. the active one) |
| `create_cycle` / `update_cycle` | Manage cycles/sprints |

Relations are resolved by name — "add a high-priority task to the Sika Deer
sprint" works without any page IDs.

## Notes

- **API version.** Pinned to `2022-06-28`, which queries by database ID and
  works with the dainframe's single-source databases. If you later split a
  database into multiple data sources, you'll need the newer data-source
  endpoints.
- **Errors** from Notion (bad enum value, page not found, permission denied)
  are returned to the model as readable text, so the bot can recover or explain
  rather than crashing the turn.
