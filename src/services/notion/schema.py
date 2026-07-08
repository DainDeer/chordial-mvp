"""the dainframe schema, encoded once.

the tools are domain-specific on purpose: the property names, the status /
priority / area vocabularies, and the relations between tasks<->projects and
tasks<->cycles are baked in here so the model picks from enums instead of
inventing notion property json. if the dainframe schema changes, this is the
one file to update.

builders (build_*) turn friendly args into notion property payloads.
readers/formatters turn notion page json back into short, promptable strings.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from config import Config

# --- database ids (resolved from config so env can override) ---------------


def tasks_db() -> str:
    return Config.NOTION_TASKS_DB_ID


def projects_db() -> str:
    return Config.NOTION_PROJECTS_DB_ID


def cycles_db() -> str:
    return Config.NOTION_CYCLES_DB_ID


# --- controlled vocabularies (must match the dainframe exactly) -------------

TASK_STATUS = ["To do", "In progress", "Done", "deprioritized"]
TASK_PRIORITY = ["high", "medium", "low"]

PROJECT_STATUS = ["Not started", "In progress", "recurring", "Done"]
PROJECT_AREA = [
    "Code", "Writing", "Art", "Other", "Health & Fitness",
    "Personal", "content creation", "music", "cooking", "job search",
]

CYCLE_STATUS = ["Upcoming", "Active", "Complete"]


# --- property builders ------------------------------------------------------
# each returns a {prop_name: value} fragment; callers merge them and drop the
# ones whose source arg was None.


def _title(prop: str, value: Optional[str]) -> dict:
    if value is None:
        return {}
    return {prop: {"title": [{"text": {"content": value}}]}}


def _rich_text(prop: str, value: Optional[str]) -> dict:
    if value is None:
        return {}
    return {prop: {"rich_text": [{"text": {"content": value}}]}}


def _select(prop: str, value: Optional[str]) -> dict:
    if value is None:
        return {}
    return {prop: {"select": {"name": value}}}


def _status(prop: str, value: Optional[str]) -> dict:
    if value is None:
        return {}
    return {prop: {"status": {"name": value}}}


def _multi_select(prop: str, values: Optional[Iterable[str]]) -> dict:
    if values is None:
        return {}
    return {prop: {"multi_select": [{"name": v} for v in values]}}


def _number(prop: str, value: Optional[float]) -> dict:
    if value is None:
        return {}
    return {prop: {"number": value}}


def _date(prop: str, start: Optional[str], end: Optional[str] = None) -> dict:
    if start is None:
        return {}
    date: dict[str, Any] = {"start": start}
    if end:
        date["end"] = end
    return {prop: {"date": date}}


def _relation(prop: str, page_ids: Optional[Iterable[str]]) -> dict:
    if page_ids is None:
        return {}
    return {prop: {"relation": [{"id": pid} for pid in page_ids]}}


def _merge(*fragments: dict) -> dict:
    out: dict = {}
    for f in fragments:
        out.update(f)
    return out


def build_task_properties(
    *,
    title: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    project_ids: Optional[list[str]] = None,
    sprint_ids: Optional[list[str]] = None,
    scheduled_start: Optional[str] = None,
    scheduled_end: Optional[str] = None,
    pom_estimate: Optional[float] = None,
) -> dict:
    return _merge(
        _title("Task", title),
        _status("Status", status),
        _select("Priority", priority),
        _relation("Project", project_ids),
        _relation("Sprint", sprint_ids),
        _date("Scheduled", scheduled_start, scheduled_end),
        _number("pom estimate", pom_estimate),
    )


def build_project_properties(
    *,
    title: Optional[str] = None,
    status: Optional[str] = None,
    area: Optional[list[str]] = None,
    description: Optional[str] = None,
) -> dict:
    return _merge(
        _title("Project", title),
        _status("Status", status),
        _multi_select("Area", area),
        _rich_text("description", description),
    )


def build_cycle_properties(
    *,
    title: Optional[str] = None,
    status: Optional[str] = None,
    dates_start: Optional[str] = None,
    dates_end: Optional[str] = None,
    goal: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    return _merge(
        _title("cycle", title),
        _status("status", status),
        _date("dates", dates_start, dates_end),
        _rich_text("cycle goal", goal),
        _rich_text("description", description),
    )


# --- filter builders --------------------------------------------------------


def _and(*clauses: Optional[dict]) -> Optional[dict]:
    real = [c for c in clauses if c]
    if not real:
        return None
    if len(real) == 1:
        return real[0]
    return {"and": real}


def task_filter(
    *,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    project_id: Optional[str] = None,
    sprint_id: Optional[str] = None,
    scheduled_on_or_after: Optional[str] = None,
    scheduled_on_or_before: Optional[str] = None,
) -> Optional[dict]:
    return _and(
        {"property": "Status", "status": {"equals": status}} if status else None,
        {"property": "Priority", "select": {"equals": priority}} if priority else None,
        {"property": "Project", "relation": {"contains": project_id}} if project_id else None,
        {"property": "Sprint", "relation": {"contains": sprint_id}} if sprint_id else None,
        {"property": "Scheduled", "date": {"on_or_after": scheduled_on_or_after}}
        if scheduled_on_or_after else None,
        {"property": "Scheduled", "date": {"on_or_before": scheduled_on_or_before}}
        if scheduled_on_or_before else None,
    )


def project_filter(
    *, status: Optional[str] = None, area: Optional[str] = None
) -> Optional[dict]:
    return _and(
        {"property": "Status", "status": {"equals": status}} if status else None,
        {"property": "Area", "multi_select": {"contains": area}} if area else None,
    )


def cycle_filter(*, status: Optional[str] = None) -> Optional[dict]:
    return _and(
        {"property": "status", "status": {"equals": status}} if status else None,
    )


def agenda_task_filter(today_iso: str) -> dict:
    """the "today picture" query: everything in progress, plus anything still
    to-do that was scheduled on or before today (i.e. due today or overdue).
    the snapshot service partitions the results by scheduled date afterwards."""
    return {
        "or": [
            {"property": "Status", "status": {"equals": "In progress"}},
            {
                "and": [
                    {"property": "Status", "status": {"equals": "To do"}},
                    {"property": "Scheduled", "date": {"on_or_before": today_iso}},
                ]
            },
        ]
    }


def title_equals_filter(title_prop: str, value: str) -> dict:
    return {"property": title_prop, "title": {"equals": value}}


# --- readers / formatters ---------------------------------------------------


def _read_plain(prop: Optional[dict]) -> str:
    """title or rich_text -> joined plain text."""
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _read_select(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    val = prop.get("select") or prop.get("status")
    return val.get("name", "") if val else ""


def _read_multi(prop: Optional[dict]) -> list[str]:
    if not prop:
        return []
    return [v.get("name", "") for v in prop.get("multi_select", [])]


def _read_number(prop: Optional[dict]) -> Optional[float]:
    return prop.get("number") if prop else None


def _read_date(prop: Optional[dict]) -> str:
    if not prop or not prop.get("date"):
        return ""
    d = prop["date"]
    start, end = d.get("start", ""), d.get("end")
    return f"{start}→{end}" if end else start


def _read_relation_ids(prop: Optional[dict]) -> list[str]:
    if not prop:
        return []
    return [r.get("id", "") for r in prop.get("relation", [])]


def page_id(page: dict) -> str:
    return page.get("id", "")


def format_task(page: dict, name_map: Optional[dict[str, str]] = None) -> str:
    """one-line summary of a task page. `name_map` (page_id -> title) resolves
    Project/Sprint relations to readable names when provided."""
    p = page.get("properties", {})
    name_map = name_map or {}
    bits = [f'"{_read_plain(p.get("Task")) or "(untitled)"}"']
    status = _read_select(p.get("Status"))
    if status:
        bits.append(f"[{status}]")
    prio = _read_select(p.get("Priority"))
    if prio:
        bits.append(f"prio={prio}")
    projects = [name_map.get(i, i[:8]) for i in _read_relation_ids(p.get("Project"))]
    if projects:
        bits.append(f"project={', '.join(projects)}")
    sprints = [name_map.get(i, i[:8]) for i in _read_relation_ids(p.get("Sprint"))]
    if sprints:
        bits.append(f"sprint={', '.join(sprints)}")
    sched = _read_date(p.get("Scheduled"))
    if sched:
        bits.append(f"scheduled={sched}")
    pom = _read_number(p.get("pom estimate"))
    if pom is not None:
        bits.append(f"pom={pom:g}")
    bits.append(f"id={page_id(page)}")
    return " ".join(bits)


def format_project(page: dict) -> str:
    p = page.get("properties", {})
    bits = [f'"{_read_plain(p.get("Project")) or "(untitled)"}"']
    status = _read_select(p.get("Status"))
    if status:
        bits.append(f"[{status}]")
    areas = _read_multi(p.get("Area"))
    if areas:
        bits.append(f"area={', '.join(areas)}")
    desc = _read_plain(p.get("description"))
    if desc:
        bits.append(f"— {desc[:120]}")
    bits.append(f"id={page_id(page)}")
    return " ".join(bits)


def format_cycle(page: dict) -> str:
    p = page.get("properties", {})
    bits = [f'"{_read_plain(p.get("cycle")) or "(untitled)"}"']
    status = _read_select(p.get("status"))
    if status:
        bits.append(f"[{status}]")
    dates = _read_date(p.get("dates"))
    if dates:
        bits.append(f"dates={dates}")
    goal = _read_plain(p.get("cycle goal"))
    if goal:
        bits.append(f"goal={goal[:80]}")
    bits.append(f"id={page_id(page)}")
    return " ".join(bits)


def title_of(page: dict, title_prop: str) -> str:
    return _read_plain(page.get("properties", {}).get(title_prop))


# --- structured rows (for the agenda snapshot, not the model) ---------------
# these return small json-safe dicts rather than promptable strings, so the
# snapshot service can partition/diff by fields (status, scheduled date, id).


def scheduled_start(page: dict) -> str:
    """the Scheduled start date as YYYY-MM-DD (drops any time component), or ''."""
    prop = page.get("properties", {}).get("Scheduled")
    if not prop or not prop.get("date"):
        return ""
    return (prop["date"].get("start") or "")[:10]


def task_row(page: dict, name_map: Optional[dict[str, str]] = None) -> dict:
    """a task page -> flat dict. `name_map` (page_id -> title) resolves the
    Project/Sprint relations to readable names when provided."""
    p = page.get("properties", {})
    name_map = name_map or {}
    projects = [name_map.get(i, i[:8]) for i in _read_relation_ids(p.get("Project"))]
    sprints = [name_map.get(i, i[:8]) for i in _read_relation_ids(p.get("Sprint"))]
    return {
        "id": page_id(page),
        "title": _read_plain(p.get("Task")) or "(untitled)",
        "status": _read_select(p.get("Status")),
        "priority": _read_select(p.get("Priority")),
        "scheduled": scheduled_start(page),
        "project": projects[0] if projects else "",
        "sprint": sprints[0] if sprints else "",
        "pom": _read_number(p.get("pom estimate")),
    }


def project_row(page: dict) -> dict:
    p = page.get("properties", {})
    return {
        "id": page_id(page),
        "title": _read_plain(p.get("Project")) or "(untitled)",
        "areas": _read_multi(p.get("Area")),
    }


def cycle_row(page: dict) -> dict:
    p = page.get("properties", {})
    return {
        "id": page_id(page),
        "title": _read_plain(p.get("cycle")) or "(untitled)",
        "dates": _read_date(p.get("dates")),
        "goal": _read_plain(p.get("cycle goal")),
    }
