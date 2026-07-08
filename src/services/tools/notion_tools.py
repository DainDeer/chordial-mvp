"""notion tools: read and write the dainframe's tasks, projects, and cycles.

domain-specific by design (see notion/schema.py for why). handlers accept
friendly arguments - status/priority names, project/sprint *names* rather than
page ids - and do the relation resolution themselves, so the model can say
"add a high-priority task to the Sika Deer sprint" without ever touching a
uuid.

the dainframe is a single shared workspace, so `user_uuid` is accepted (the
loop injects it) but unused here.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from src.providers.ai.types import ToolDef
from src.services.notion import get_client
from src.services.notion import schema as S
from src.services.notion.snapshot_service import invalidate_all
from config import Config
from .base import Tool

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


def _looks_like_id(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


async def _name_map(db_id: str, title_prop: str) -> dict[str, str]:
    """{page_id -> title} for a whole (small) database, for resolving
    relations to readable names when formatting."""
    client = get_client()
    rows = await client.query_all(db_id, limit=100)
    return {S.page_id(r): S.title_of(r, title_prop) for r in rows}


async def _resolve_by_title(db_id: str, title_prop: str, name: str) -> Optional[str]:
    """find a page id by exact title, then by case-insensitive substring."""
    client = get_client()
    exact = await client.query_all(
        db_id, filter=S.title_equals_filter(title_prop, name), limit=1
    )
    if exact:
        return S.page_id(exact[0])
    rows = await client.query_all(db_id, limit=100)
    needle = name.strip().lower()
    for r in rows:
        if needle in S.title_of(r, title_prop).lower():
            return S.page_id(r)
    return None


async def _resolve_names_to_ids(
    db_id: str, title_prop: str, names: Optional[list[str]]
) -> tuple[Optional[list[str]], list[str]]:
    """map a list of names to page ids. returns (ids_or_None, unresolved)."""
    if names is None:
        return None, []
    ids: list[str] = []
    missing: list[str] = []
    for n in names:
        pid = n if _looks_like_id(n) else await _resolve_by_title(db_id, title_prop, n)
        (ids.append(pid) if pid else missing.append(n))
    return ids, missing


async def _target_id(db_id: str, title_prop: str, identifier: str) -> Optional[str]:
    if _looks_like_id(identifier):
        return identifier
    return await _resolve_by_title(db_id, title_prop, identifier)


def _cap(n: Optional[int]) -> int:
    hi = Config.NOTION_MAX_PAGE_SIZE
    if not n:
        return hi
    return max(1, min(int(n), hi))


# ============================ TASKS ========================================


async def _list_tasks(tool_input: dict, user_uuid: str) -> str:
    client = get_client()
    project_id = None
    if tool_input.get("project"):
        project_id = await _resolve_by_title(S.projects_db(), "Project", tool_input["project"])
        if not project_id:
            return f"no project named '{tool_input['project']}' found."
    sprint_id = None
    if tool_input.get("sprint"):
        sprint_id = await _resolve_by_title(S.cycles_db(), "cycle", tool_input["sprint"])
        if not sprint_id:
            return f"no sprint/cycle named '{tool_input['sprint']}' found."

    filt = S.task_filter(
        status=tool_input.get("status"),
        priority=tool_input.get("priority"),
        project_id=project_id,
        sprint_id=sprint_id,
        scheduled_on_or_after=tool_input.get("scheduled_on_or_after"),
        scheduled_on_or_before=tool_input.get("scheduled_on_or_before"),
    )
    rows = await client.query_all(
        S.tasks_db(), filter=filt,
        sorts=[{"property": "Scheduled", "direction": "ascending"}],
        limit=_cap(tool_input.get("limit")),
    )
    if not rows:
        return "no tasks matched."
    # resolve relation names for readability (two small queries)
    projects = await _name_map(S.projects_db(), "Project")
    cycles = await _name_map(S.cycles_db(), "cycle")
    name_map = {**projects, **cycles}
    return f"{len(rows)} task(s):\n" + "\n".join(
        f"- {S.format_task(r, name_map)}" for r in rows
    )


async def _create_task(tool_input: dict, user_uuid: str) -> str:
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a task needs a title."

    project_ids, miss_p = await _resolve_names_to_ids(
        S.projects_db(), "Project",
        [tool_input["project"]] if tool_input.get("project") else None,
    )
    sprint_ids, miss_s = await _resolve_names_to_ids(
        S.cycles_db(), "cycle",
        [tool_input["sprint"]] if tool_input.get("sprint") else None,
    )
    unresolved = miss_p + miss_s
    if unresolved:
        return f"couldn't find these by name: {', '.join(unresolved)}. create them first or check the name."

    props = S.build_task_properties(
        title=title,
        status=tool_input.get("status") or "To do",
        priority=tool_input.get("priority"),
        project_ids=project_ids,
        sprint_ids=sprint_ids,
        scheduled_start=tool_input.get("scheduled_date"),
        pom_estimate=tool_input.get("pom_estimate"),
    )
    page = await get_client().create_page(S.tasks_db(), props)
    invalidate_all()  # the agenda picture just changed
    return f"created task \"{title}\" (id={S.page_id(page)})."


async def _update_task(tool_input: dict, user_uuid: str) -> str:
    ident = (tool_input.get("task") or "").strip()
    if not ident:
        return "which task? pass a task title or id."
    page_id = await _target_id(S.tasks_db(), "Task", ident)
    if not page_id:
        return f"no task matching '{ident}'."

    project_ids, miss_p = await _resolve_names_to_ids(
        S.projects_db(), "Project",
        [tool_input["project"]] if tool_input.get("project") else None,
    )
    sprint_ids, miss_s = await _resolve_names_to_ids(
        S.cycles_db(), "cycle",
        [tool_input["sprint"]] if tool_input.get("sprint") else None,
    )
    if miss_p + miss_s:
        return f"couldn't find these by name: {', '.join(miss_p + miss_s)}."

    props = S.build_task_properties(
        title=tool_input.get("new_title"),
        status=tool_input.get("status"),
        priority=tool_input.get("priority"),
        project_ids=project_ids,
        sprint_ids=sprint_ids,
        scheduled_start=tool_input.get("scheduled_date"),
        pom_estimate=tool_input.get("pom_estimate"),
    )
    if not props:
        return "nothing to update - pass at least one field to change."
    await get_client().update_page(page_id, props)
    invalidate_all()  # the agenda picture just changed
    return f"updated task (id={page_id}): {', '.join(props.keys())}."


# ============================ PROJECTS =====================================


async def _list_projects(tool_input: dict, user_uuid: str) -> str:
    rows = await get_client().query_all(
        S.projects_db(),
        filter=S.project_filter(status=tool_input.get("status"), area=tool_input.get("area")),
        limit=_cap(tool_input.get("limit")),
    )
    if not rows:
        return "no projects matched."
    return f"{len(rows)} project(s):\n" + "\n".join(
        f"- {S.format_project(r)}" for r in rows
    )


async def _create_project(tool_input: dict, user_uuid: str) -> str:
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a project needs a title."
    props = S.build_project_properties(
        title=title,
        status=tool_input.get("status") or "Not started",
        area=tool_input.get("area"),
        description=tool_input.get("description"),
    )
    page = await get_client().create_page(S.projects_db(), props)
    invalidate_all()  # the agenda picture just changed
    return f"created project \"{title}\" (id={S.page_id(page)})."


async def _update_project(tool_input: dict, user_uuid: str) -> str:
    ident = (tool_input.get("project") or "").strip()
    if not ident:
        return "which project? pass a project title or id."
    page_id = await _target_id(S.projects_db(), "Project", ident)
    if not page_id:
        return f"no project matching '{ident}'."
    props = S.build_project_properties(
        title=tool_input.get("new_title"),
        status=tool_input.get("status"),
        area=tool_input.get("area"),
        description=tool_input.get("description"),
    )
    if not props:
        return "nothing to update - pass at least one field to change."
    await get_client().update_page(page_id, props)
    invalidate_all()  # the agenda picture just changed
    return f"updated project (id={page_id}): {', '.join(props.keys())}."


# ============================ CYCLES =======================================


async def _list_cycles(tool_input: dict, user_uuid: str) -> str:
    rows = await get_client().query_all(
        S.cycles_db(),
        filter=S.cycle_filter(status=tool_input.get("status")),
        sorts=[{"property": "dates", "direction": "descending"}],
        limit=_cap(tool_input.get("limit")),
    )
    if not rows:
        return "no cycles matched."
    return f"{len(rows)} cycle(s):\n" + "\n".join(
        f"- {S.format_cycle(r)}" for r in rows
    )


async def _create_cycle(tool_input: dict, user_uuid: str) -> str:
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a cycle needs a title."
    props = S.build_cycle_properties(
        title=title,
        status=tool_input.get("status") or "Upcoming",
        dates_start=tool_input.get("start_date"),
        dates_end=tool_input.get("end_date"),
        goal=tool_input.get("goal"),
        description=tool_input.get("description"),
    )
    page = await get_client().create_page(S.cycles_db(), props)
    invalidate_all()  # the agenda picture just changed
    return f"created cycle \"{title}\" (id={S.page_id(page)})."


async def _update_cycle(tool_input: dict, user_uuid: str) -> str:
    ident = (tool_input.get("cycle") or "").strip()
    if not ident:
        return "which cycle? pass a cycle title or id."
    page_id = await _target_id(S.cycles_db(), "cycle", ident)
    if not page_id:
        return f"no cycle matching '{ident}'."
    props = S.build_cycle_properties(
        title=tool_input.get("new_title"),
        status=tool_input.get("status"),
        dates_start=tool_input.get("start_date"),
        dates_end=tool_input.get("end_date"),
        goal=tool_input.get("goal"),
        description=tool_input.get("description"),
    )
    if not props:
        return "nothing to update - pass at least one field to change."
    await get_client().update_page(page_id, props)
    invalidate_all()  # the agenda picture just changed
    return f"updated cycle (id={page_id}): {', '.join(props.keys())}."


# ============================ TOOL DEFS ====================================

LIST_TASKS = Tool(
    definition=ToolDef(
        name="list_tasks",
        description=(
            "List tasks from the dainframe (the user's Notion). Filter by any "
            "combination of status, priority, project name, sprint/cycle name, "
            "and scheduled date range. You already see a compact agenda summary "
            "with each message; use this to look beyond it - a specific filter, "
            "'what's due this week', the full backlog, etc. Results are sorted "
            "by scheduled date and each line ends with the task's id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": S.TASK_STATUS,
                           "description": "Only tasks with this status."},
                "priority": {"type": "string", "enum": S.TASK_PRIORITY,
                             "description": "Only tasks with this priority."},
                "project": {"type": "string", "description": "Project name to filter by."},
                "sprint": {"type": "string", "description": "Sprint/cycle name to filter by."},
                "scheduled_on_or_after": {"type": "string",
                                          "description": "ISO date (YYYY-MM-DD); tasks scheduled on/after this."},
                "scheduled_on_or_before": {"type": "string",
                                           "description": "ISO date (YYYY-MM-DD); tasks scheduled on/before this."},
                "limit": {"type": "integer", "description": "Max rows to return."},
            },
        },
    ),
    handler=_list_tasks,
)

CREATE_TASK = Tool(
    definition=ToolDef(
        name="create_task",
        description=(
            "Create a new task in the dainframe. Only 'title' is required; "
            "defaults status to 'To do'. Link it to a project and/or sprint by "
            "their names (they must already exist). Use when the user wants to "
            "capture a to-do."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The task text (the title)."},
                "status": {"type": "string", "enum": S.TASK_STATUS},
                "priority": {"type": "string", "enum": S.TASK_PRIORITY},
                "project": {"type": "string", "description": "Existing project name to link to."},
                "sprint": {"type": "string", "description": "Existing sprint/cycle name to link to."},
                "scheduled_date": {"type": "string", "description": "ISO date (YYYY-MM-DD) to schedule it."},
                "pom_estimate": {"type": "number", "description": "Estimated pomodoros."},
            },
            "required": ["title"],
        },
    ),
    handler=_create_task,
)

UPDATE_TASK = Tool(
    definition=ToolDef(
        name="update_task",
        description=(
            "Update an existing task. Identify it by title or id via 'task'. "
            "Pass only the fields to change: use this to mark something done "
            "(status='Done'), reprioritize, reschedule, re-link, or rename "
            "(new_title). Relations are set by name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Title or id of the task to update."},
                "new_title": {"type": "string", "description": "Rename the task."},
                "status": {"type": "string", "enum": S.TASK_STATUS},
                "priority": {"type": "string", "enum": S.TASK_PRIORITY},
                "project": {"type": "string", "description": "Project name to (re)link to."},
                "sprint": {"type": "string", "description": "Sprint/cycle name to (re)link to."},
                "scheduled_date": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
                "pom_estimate": {"type": "number"},
            },
            "required": ["task"],
        },
    ),
    handler=_update_task,
)

LIST_PROJECTS = Tool(
    definition=ToolDef(
        name="list_projects",
        description="List projects from the dainframe, optionally filtered by status or area.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": S.PROJECT_STATUS},
                "area": {"type": "string", "enum": S.PROJECT_AREA,
                         "description": "Only projects tagged with this area."},
                "limit": {"type": "integer"},
            },
        },
    ),
    handler=_list_projects,
)

CREATE_PROJECT = Tool(
    definition=ToolDef(
        name="create_project",
        description=(
            "Create a new project in the dainframe. Only 'title' is required; "
            "defaults status to 'Not started'. 'area' is a list of tags."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "status": {"type": "string", "enum": S.PROJECT_STATUS},
                "area": {"type": "array", "items": {"type": "string", "enum": S.PROJECT_AREA}},
                "description": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    handler=_create_project,
)

UPDATE_PROJECT = Tool(
    definition=ToolDef(
        name="update_project",
        description="Update a project (identify by title or id via 'project'). Pass only fields to change.",
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Title or id of the project."},
                "new_title": {"type": "string"},
                "status": {"type": "string", "enum": S.PROJECT_STATUS},
                "area": {"type": "array", "items": {"type": "string", "enum": S.PROJECT_AREA}},
                "description": {"type": "string"},
            },
            "required": ["project"],
        },
    ),
    handler=_update_project,
)

LIST_CYCLES = Tool(
    definition=ToolDef(
        name="list_cycles",
        description=(
            "List cycles/sprints from the dainframe, newest first, optionally "
            "filtered by status. Use to find the current sprint (status='Active')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": S.CYCLE_STATUS},
                "limit": {"type": "integer"},
            },
        },
    ),
    handler=_list_cycles,
)

CREATE_CYCLE = Tool(
    definition=ToolDef(
        name="create_cycle",
        description=(
            "Create a new cycle/sprint in the dainframe. Only 'title' is "
            "required; defaults status to 'Upcoming'. Give it a date range and "
            "a goal when known."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "status": {"type": "string", "enum": S.CYCLE_STATUS},
                "start_date": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
                "end_date": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
                "goal": {"type": "string", "description": "The cycle goal."},
                "description": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    handler=_create_cycle,
)

UPDATE_CYCLE = Tool(
    definition=ToolDef(
        name="update_cycle",
        description="Update a cycle/sprint (identify by title or id via 'cycle'). Pass only fields to change.",
        input_schema={
            "type": "object",
            "properties": {
                "cycle": {"type": "string", "description": "Title or id of the cycle."},
                "new_title": {"type": "string"},
                "status": {"type": "string", "enum": S.CYCLE_STATUS},
                "start_date": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
                "end_date": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
                "goal": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["cycle"],
        },
    ),
    handler=_update_cycle,
)


NOTION_TOOLS = [
    LIST_TASKS, CREATE_TASK, UPDATE_TASK,
    LIST_PROJECTS, CREATE_PROJECT, UPDATE_PROJECT,
    LIST_CYCLES, CREATE_CYCLE, UPDATE_CYCLE,
]
