"""the web focus view server: today's tasks + the pomodoro bar, on localhost.

deployment shape (decided 2026-07-28): an aiohttp app inside the main
process, supervised by _run_services exactly like the platform interfaces
and the pulse - aiohttp is already a dependency (discord.py rides on it)
and runs natively on the same asyncio loop, so there's no second process,
no build step, and no new server to deploy. the frontend is plain static
files served from src/web/static/ - edit, refresh, done.

not a chat platform: the service intentionally does NOT register with the
MessageRouter (nothing here sends messages). it composes the two workspace
stores - WorkspaceStore for task truth, FocusStore for the clock - the same
way the tool layer would, so the web buttons and the chat tools can never
disagree about what "complete" means.

api surface (all JSON, all single-user):
    GET  /api/today                  the whole picture: buckets + focus + config
    POST /api/focus/start            {"task_id": int} start/resume/switch
    POST /api/focus/pause            bank the running clock
    POST /api/tasks/{id}/status      {"status": "done"|"deprioritized"|...}
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiohttp import web

from config import Config
from src.database.database import get_db
from src.database.models import User
from src.services.workspace import get_store, vocab
from src.services.workspace.agenda import user_today
from src.services.workspace.focus import FocusStore
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebUserError(RuntimeError):
    """the page can't tell whose workspace to show - config must say."""


def resolve_web_user() -> str:
    """whose workspace the page shows. WEB_USER_UUID wins when set; else the
    sole active real (non-test) user. anything ambiguous is an error the api
    surfaces per-request - a misconfigured page must never guess a person."""
    if Config.WEB_USER_UUID:
        with get_db() as db:
            row = db.query(User.uuid).filter(
                User.uuid == Config.WEB_USER_UUID).first()
        if row is None:
            raise WebUserError(
                f"WEB_USER_UUID {Config.WEB_USER_UUID!r} matches no user")
        return Config.WEB_USER_UUID
    with get_db() as db:
        uuids = [u[0] for u in db.query(User.uuid).filter(
            User.is_active.is_(True), User.is_test.is_(False)).all()]
    if len(uuids) == 1:
        return uuids[0]
    if not uuids:
        raise WebUserError("no active users yet - say hi to chordial first")
    raise WebUserError(
        f"{len(uuids)} active users - set WEB_USER_UUID to pick one")


def _task_row(t: dict) -> dict:
    """the fields the page renders. numeric id rides along because the focus
    api keys on it; public_id is for humans and future chat deep-links."""
    return {
        "id": t["id"], "public_id": t["public_id"], "title": t["title"],
        "status": t["status"], "priority": t["priority"],
        "scheduled": t["scheduled"], "window": t["window"],
        "pom_estimate": t["pom_estimate"], "plan_title": t["plan_title"],
        "helper": t["helper"], "description": t["description"],
    }


class WebService:
    """the supervised service. start() binds and then parks until stop() -
    _run_services treats a returning service task as a death, so the park is
    load-bearing, same as the platform interfaces."""

    platform = "web"

    def __init__(self, store=None, focus: Optional[FocusStore] = None,
                 user_resolver=resolve_web_user):
        self.store = store or get_store()
        self.focus = focus or FocusStore()
        self._resolve_user = user_resolver
        self._runner: Optional[web.AppRunner] = None
        self._stop_event: Optional[asyncio.Event] = None

    # --- app assembly --------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/today", self._api_today)
        app.router.add_post("/api/focus/start", self._api_focus_start)
        app.router.add_post("/api/focus/pause", self._api_focus_pause)
        app.router.add_post("/api/tasks/{task_id}/status", self._api_task_status)
        app.router.add_static("/static", STATIC_DIR)
        return app

    async def start(self):
        self._stop_event = asyncio.Event()
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, Config.WEB_HOST, Config.WEB_PORT)
        await site.start()
        logger.info("web focus view on http://%s:%s",
                    Config.WEB_HOST, Config.WEB_PORT)
        await self._stop_event.wait()

    async def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- handlers -------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _api_today(self, request: web.Request) -> web.Response:
        return await asyncio.to_thread(self._today_payload)

    def _today_payload(self) -> web.Response:
        try:
            user_uuid = self._resolve_user()
        except WebUserError as e:
            return _error(str(e), status=409)

        today = user_today(user_uuid)
        today_iso = today.isoformat()
        with get_db() as db:
            name = db.query(User.preferred_name).filter(
                User.uuid == user_uuid).scalar()

        # same bucketing as the agenda payload (today / overdue / started-but-
        # undated), but rows keep their numeric ids for the focus api
        tasks = self.store.list_tasks(user_uuid)   # open only, scheduled order
        buckets = {"overdue": [], "today": [], "in_progress": []}
        for t in tasks:
            sched = t["scheduled"]
            if sched == today_iso:
                buckets["today"].append(_task_row(t))
            elif sched and sched < today_iso:
                buckets["overdue"].append(_task_row(t))
            elif t["status"] == "in_progress":
                buckets["in_progress"].append(_task_row(t))

        snap = self.focus.snapshot(user_uuid)
        return web.json_response({
            "today": today_iso,
            "user": {"name": name},
            "pom_minutes": Config.POM_MINUTES,
            "buckets": buckets,
            "focus": {
                "active_task_id": snap["active_task_id"],
                # json object keys are strings; the frontend knows
                "seconds": {str(k): v for k, v in snap["seconds"].items()},
            },
            "server_time": utc_now().isoformat(),
        })

    async def _api_focus_start(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        task_id = body.get("task_id") if isinstance(body, dict) else None
        if not isinstance(task_id, int):
            return _error("task_id (int) required")
        return await asyncio.to_thread(self._focus_start, task_id)

    def _focus_start(self, task_id: int) -> web.Response:
        try:
            user_uuid = self._resolve_user()
        except WebUserError as e:
            return _error(str(e), status=409)
        try:
            self.focus.start(user_uuid, task_id)
            # starting the clock means the work started: same transition the
            # chat tools make. idempotent when it's already in_progress.
            task = self.store.update_task(user_uuid, task_id,
                                          status="in_progress")
        except ValueError as e:
            return _error(str(e), status=404 if "not found" in str(e) else 400)
        return web.json_response({"ok": True, "task": _task_row(task)})

    async def _api_focus_pause(self, request: web.Request) -> web.Response:
        return await asyncio.to_thread(self._focus_pause)

    def _focus_pause(self) -> web.Response:
        try:
            user_uuid = self._resolve_user()
        except WebUserError as e:
            return _error(str(e), status=409)
        self.focus.pause(user_uuid)
        return web.json_response({"ok": True})

    async def _api_task_status(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        status = body.get("status") if isinstance(body, dict) else None
        if not isinstance(status, str):
            return _error("status (string) required")
        try:
            task_id = int(request.match_info["task_id"])
        except ValueError:
            return _error("task id must be an integer")
        return await asyncio.to_thread(self._task_status, task_id, status)

    def _task_status(self, task_id: int, status: str) -> web.Response:
        try:
            user_uuid = self._resolve_user()
        except WebUserError as e:
            return _error(str(e), status=409)
        try:
            canonical = vocab.canonical_status("task", status)
            if vocab.is_closed_status("task", canonical):
                # closing banks the clock first - a done task never accrues
                self.focus.stop(user_uuid, task_id)
            task = self.store.update_task(user_uuid, task_id, status=canonical)
        except ValueError as e:
            return _error(str(e), status=404 if "not found" in str(e) else 400)
        return web.json_response({"ok": True, "task": _task_row(task)})


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _json_body(request: web.Request):
    try:
        return await request.json()
    except Exception:
        return None
