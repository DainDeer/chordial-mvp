"""the agenda snapshot: a cached, promptable view of the user's notion.

the reactive notion tools (list_tasks, ...) let the model *pull* task data when
it decides to. this service is the *push* side: it keeps a small "today picture"
per user - due/overdue tasks, in-progress work, the active cycle, live projects -
in the `agenda_snapshots` table, pre-rendered into a compact digest.

the split that matters:
- `refresh` / `ensure_fresh` hit notion (3 queries) and are only ever called off
  the background scheduler loop - never in the chat path.
- `get_digest` / `get_payload` are pure db reads (microseconds), safe to call
  while building a reply, so notion latency never sits in front of the user.

notion writes made through the tools call `invalidate_all()`, which flags every
snapshot stale so the next background pass re-fetches - the model already saw the
fresh data in its own tool result, so there's no need to re-query eagerly.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from config import Config
from src.database.database import get_db
from src.database.models import AgendaSnapshot, User
from src.services.notion import schema as S
from src.services.notion.client import NotionClient, NotionError, get_client
from src.utils.timezone_utils import utc_now, to_user_timezone

logger = logging.getLogger(__name__)

# per-section caps so the digest stays ~150-400 tokens regardless of backlog.
_MAX_TODAY = 8
_MAX_OVERDUE = 6
_MAX_IN_PROGRESS = 5

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


def _pretty_date(iso: str) -> str:
    """'2026-07-14' -> 'jul 14'. returns the input unchanged if unparseable."""
    try:
        y, m, d = iso[:10].split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}"
    except (ValueError, IndexError):
        return iso


class AgendaSnapshotService:
    def __init__(
        self,
        ttl_minutes: Optional[int] = None,
        client_factory: Callable[[], NotionClient] = get_client,
    ):
        self.ttl = timedelta(minutes=ttl_minutes if ttl_minutes is not None
                             else Config.AGENDA_TTL_MINUTES)
        self._client_factory = client_factory

    # --- notion-touching (background only) ---------------------------------

    async def refresh(self, user_uuid: str) -> Optional[dict]:
        """re-query notion (3 calls) and rewrite this user's snapshot row.

        on a notion error the last good digest is kept, the error recorded, and
        the row left stale; returns the fresh payload, or None on failure."""
        tz = self._user_timezone(user_uuid)
        today = to_user_timezone(utc_now(), tz).date()
        client = self._client_factory()

        try:
            task_pages = await client.query_all(
                S.tasks_db(),
                filter=S.agenda_task_filter(today.isoformat()),
                sorts=[{"property": "Scheduled", "direction": "ascending"}],
                limit=50,
            )
            cycle_pages = await client.query_all(
                S.cycles_db(), filter=S.cycle_filter(status="Active"), limit=2,
            )
            project_pages = await client.query_all(
                S.projects_db(), filter=S.project_filter(status="In progress"), limit=15,
            )
        except NotionError as e:
            logger.warning("agenda refresh failed for %s: %s", user_uuid, e)
            self._record_error(user_uuid, str(e))
            return None

        payload = self._build_payload(task_pages, cycle_pages, project_pages, today)
        digest = self._render_digest(payload)
        self._write(user_uuid, payload, digest)
        return payload

    async def ensure_fresh(self, user_uuid: str) -> None:
        """refresh iff the snapshot is missing, flagged stale, or past its ttl.
        the only method background passes need before reading."""
        with get_db() as db:
            row = db.query(AgendaSnapshot).filter(
                AgendaSnapshot.user_uuid == user_uuid
            ).first()
            fresh = (
                row is not None
                and not row.is_stale
                and row.refreshed_at is not None
                and (utc_now() - row.refreshed_at) < self.ttl
            )
        if not fresh:
            await self.refresh(user_uuid)

    # --- pure db reads (chat-path safe) ------------------------------------

    def get_digest(self, user_uuid: str) -> Optional[str]:
        """the pre-rendered agenda text, or None. NEVER touches notion."""
        with get_db() as db:
            row = db.query(AgendaSnapshot).filter(
                AgendaSnapshot.user_uuid == user_uuid
            ).first()
            return row.digest if row else None

    def get_payload(self, user_uuid: str) -> Optional[dict]:
        """the structured agenda rows, or None. NEVER touches notion."""
        with get_db() as db:
            row = db.query(AgendaSnapshot).filter(
                AgendaSnapshot.user_uuid == user_uuid
            ).first()
            return dict(row.payload) if row and row.payload else None

    # --- payload assembly --------------------------------------------------

    def _build_payload(self, task_pages, cycle_pages, project_pages, today: date) -> dict:
        # relation names come from the cycle/project pages we already fetched -
        # no extra round trips. tasks pointing at a not-in-progress project fall
        # back to an id stub, which is fine in a digest.
        name_map = {S.page_id(p): S.title_of(p, "Project") for p in project_pages}
        name_map.update({S.page_id(p): S.title_of(p, "cycle") for p in cycle_pages})

        today_iso = today.isoformat()
        tasks_today, tasks_overdue, tasks_in_progress = [], [], []
        for page in task_pages:
            row = S.task_row(page, name_map)
            sched = row["scheduled"]
            if sched and sched == today_iso:
                tasks_today.append(row)
            elif sched and sched < today_iso:
                tasks_overdue.append(row)
            else:
                # in-progress with no date (or a future one) - active but not due
                tasks_in_progress.append(row)

        cycles = [S.cycle_row(p) for p in cycle_pages]
        return {
            "cycle": cycles[0] if cycles else None,
            "projects": [S.project_row(p) for p in project_pages],
            "tasks_today": tasks_today,
            "tasks_overdue": tasks_overdue,
            "tasks_in_progress": tasks_in_progress,
            "done_today": [],  # populated by the evening pass (PR2)
        }

    def _render_digest(self, payload: dict) -> str:
        today = payload.get("tasks_today") or []
        overdue = payload.get("tasks_overdue") or []
        in_progress = payload.get("tasks_in_progress") or []
        cycle = payload.get("cycle")

        if not (today or overdue or in_progress or cycle):
            return "notion agenda: clear - nothing scheduled today, nothing overdue."

        lines = ["notion agenda (background awareness - the user hasn't seen this):"]

        if cycle:
            bits = [f'cycle: "{cycle.get("title", "")}"']
            dates = cycle.get("dates") or ""
            if "→" in dates:
                bits.append(f"ends {_pretty_date(dates.split('→')[1])}")
            goal = cycle.get("goal")
            if goal:
                bits.append(f"- goal: {goal}")
            lines.append(" ".join(bits))

        if today:
            lines.append(f"today ({len(today)}): " + self._join_tasks(
                today, _MAX_TODAY, self._fmt_today))
        if overdue:
            lines.append(f"overdue ({len(overdue)}): " + self._join_tasks(
                overdue, _MAX_OVERDUE, self._fmt_overdue))
        if in_progress:
            lines.append(f"also in progress ({len(in_progress)}): " + self._join_tasks(
                in_progress, _MAX_IN_PROGRESS, self._fmt_in_progress))
        elif not (today or overdue):
            # a cycle but no live tasks - say so rather than leaving it ambiguous
            lines.append("nothing scheduled today, nothing overdue.")

        return "\n".join(lines)

    @staticmethod
    def _join_tasks(rows, cap, fmt) -> str:
        shown = " / ".join(fmt(r) for r in rows[:cap])
        extra = len(rows) - cap
        return f"{shown} …and {extra} more" if extra > 0 else shown

    @staticmethod
    def _fmt_today(row) -> str:
        meta = [row.get("status") or ""]
        if row.get("priority"):
            meta.append(row["priority"])
        meta_str = ", ".join(m for m in meta if m)
        base = f'"{row.get("title", "")}"'
        return f"{base} [{meta_str}]" if meta_str else base

    @staticmethod
    def _fmt_overdue(row) -> str:
        base = f'"{row.get("title", "")}"'
        sched = row.get("scheduled")
        return f"{base} (was {_pretty_date(sched)})" if sched else base

    @staticmethod
    def _fmt_in_progress(row) -> str:
        base = f'"{row.get("title", "")}"'
        proj = row.get("project")
        return f"{base} (project: {proj})" if proj else base

    # --- row i/o -----------------------------------------------------------

    def _user_timezone(self, user_uuid: str) -> str:
        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            return (user.timezone if user and user.timezone else "UTC")

    def _write(self, user_uuid: str, payload: dict, digest: str) -> None:
        with get_db() as db:
            row = db.query(AgendaSnapshot).filter(
                AgendaSnapshot.user_uuid == user_uuid
            ).first()
            if row is None:
                row = AgendaSnapshot(user_uuid=user_uuid)
                db.add(row)
            row.payload = payload
            row.digest = digest
            row.refreshed_at = utc_now()
            row.is_stale = False
            row.last_error = None

    def _record_error(self, user_uuid: str, message: str) -> None:
        with get_db() as db:
            row = db.query(AgendaSnapshot).filter(
                AgendaSnapshot.user_uuid == user_uuid
            ).first()
            if row is None:
                row = AgendaSnapshot(user_uuid=user_uuid)
                db.add(row)
            # keep any existing digest; just record why the refresh failed
            row.is_stale = True
            row.last_error = message[:500]


def invalidate_all() -> None:
    """flag every snapshot stale so the next background pass re-fetches. called
    by the notion write tools after a successful create/update (single shared
    workspace -> a write can affect any user's picture). guarded: never raises,
    so a tool's success is never turned into a failure by bookkeeping."""
    try:
        with get_db() as db:
            db.query(AgendaSnapshot).update(
                {AgendaSnapshot.is_stale: True}, synchronize_session=False
            )
    except Exception as e:
        logger.debug("agenda snapshot invalidation skipped: %s", e)
