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

api surface (all JSON):
    GET  /api/today                  the whole picture: buckets + focus + config
    POST /api/focus/start            {"task_id": int} start/resume/switch
    POST /api/focus/pause            bank the running clock
    POST /api/tasks/{id}/status      {"status": "done"|"deprioritized"|...}
    POST /api/login/redeem           {"code": str} -> session cookie
    POST /api/logout                 clear the session

two deployment modes, ONE switch (Config.WEB_PUBLIC_URL - see config.py):
localhost keeps the original trust-the-interface behavior with the
single-user resolver; public mode requires a chordial-issued session on
every page and api call, and each request acts as the session's OWN user -
the multi-user seam the localhost resolver deliberately punted on.
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
from src.web import auth

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
        self._limiter = auth.RateLimiter()
        self._runner: Optional[web.AppRunner] = None
        self._stop_event: Optional[asyncio.Event] = None

    # --- app assembly --------------------------------------------------------

    def build_app(self) -> web.Application:
        if Config.web_auth_enabled() and not Config.WEB_SESSION_SECRET:
            # fail at startup, not at the first login attempt
            raise RuntimeError(
                "WEB_PUBLIC_URL is set but WEB_SESSION_SECRET is not - "
                "generate one: openssl rand -hex 32")
        app = web.Application(middlewares=[self._origin_guard])
        app.router.add_get("/", self._index)
        app.router.add_get("/login", self._login_page)
        app.router.add_post("/api/login/redeem", self._api_login_redeem)
        app.router.add_post("/api/logout", self._api_logout)
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

    # --- cross-origin writes --------------------------------------------------

    @web.middleware
    async def _origin_guard(self, request: web.Request, handler):
        """public mode refuses state-changing requests from any other origin.
        SameSite=Lax alone doesn't cover a sibling subdomain on the same
        personal domain (the portfolio site could POST here with cookies, and
        aiohttp parses json out of text/plain "simple" requests). browsers
        always send Origin on cross-origin POSTs, so a mismatch is decisive;
        absent means non-browser (curl), which csrf doesn't apply to."""
        if Config.web_auth_enabled() and request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin is not None and origin != Config.WEB_PUBLIC_URL:
                return _error("cross-origin request refused", status=403)
        return await handler(request)

    # --- who is asking --------------------------------------------------------

    def _session_user(self, request: web.Request) -> Optional[str]:
        return auth.verify_session(request.cookies.get(auth.SESSION_COOKIE))

    async def _request_user(self, request: web.Request) -> str:
        """the user this request acts as. public mode: the session's user,
        or 401. localhost mode: the configured resolver, exactly as before.
        raises aiohttp HTTP errors; handlers just await it first."""
        if Config.web_auth_enabled():
            user_uuid = self._session_user(request)
            if user_uuid is None:
                raise web.HTTPUnauthorized(
                    text='{"error": "not logged in - ask chordial for a '
                         'login code"}',
                    content_type="application/json")
            return user_uuid
        try:
            return await asyncio.to_thread(self._resolve_user)
        except WebUserError as e:
            raise web.HTTPConflict(
                text='{"error": "%s"}' % str(e).replace('"', "'"),
                content_type="application/json")

    # --- login ----------------------------------------------------------------

    async def _login_page(self, request: web.Request) -> web.StreamResponse:
        # a valid session skips the page; ?code= stays in the url for the
        # page script to auto-redeem (the GET itself must never redeem -
        # link-preview prefetchers would burn the single-use code)
        if not Config.web_auth_enabled() or self._session_user(request):
            raise web.HTTPFound("/")
        return web.FileResponse(STATIC_DIR / "login.html")

    async def _api_login_redeem(self, request: web.Request) -> web.Response:
        if not Config.web_auth_enabled():
            return _error("login is not enabled on this deployment", status=404)
        if not self._limiter.allow(_client_ip(request)):
            return _error("too many attempts - wait a few minutes", status=429)
        body = await _json_body(request)
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            return _error("code (string) required")
        user_uuid = await asyncio.to_thread(auth.redeem_login_code, code)
        if user_uuid is None:
            return _error("that code is invalid or expired - ask chordial "
                          "for a fresh one", status=401)
        response = web.json_response({"ok": True})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.mint_session(user_uuid),
            max_age=Config.WEB_SESSION_DAYS * 86400,
            httponly=True,
            samesite="Lax",
            secure=Config.WEB_PUBLIC_URL.startswith("https"),
            path="/",
        )
        return response

    async def _api_logout(self, request: web.Request) -> web.Response:
        response = web.json_response({"ok": True})
        response.del_cookie(auth.SESSION_COOKIE, path="/")
        return response

    # --- handlers -------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.StreamResponse:
        if Config.web_auth_enabled() and self._session_user(request) is None:
            raise web.HTTPFound("/login")
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _api_today(self, request: web.Request) -> web.Response:
        user_uuid = await self._request_user(request)
        return await asyncio.to_thread(self._today_payload, user_uuid)

    def _today_payload(self, user_uuid: str) -> web.Response:
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
        user_uuid = await self._request_user(request)
        body = await _json_body(request)
        task_id = body.get("task_id") if isinstance(body, dict) else None
        if not isinstance(task_id, int):
            return _error("task_id (int) required")
        return await asyncio.to_thread(self._focus_start, user_uuid, task_id)

    def _focus_start(self, user_uuid: str, task_id: int) -> web.Response:
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
        user_uuid = await self._request_user(request)
        return await asyncio.to_thread(self._focus_pause, user_uuid)

    def _focus_pause(self, user_uuid: str) -> web.Response:
        self.focus.pause(user_uuid)
        return web.json_response({"ok": True})

    async def _api_task_status(self, request: web.Request) -> web.Response:
        user_uuid = await self._request_user(request)
        body = await _json_body(request)
        status = body.get("status") if isinstance(body, dict) else None
        if not isinstance(status, str):
            return _error("status (string) required")
        try:
            task_id = int(request.match_info["task_id"])
        except ValueError:
            return _error("task id must be an integer")
        return await asyncio.to_thread(self._task_status, user_uuid, task_id, status)

    def _task_status(self, user_uuid: str, task_id: int, status: str) -> web.Response:
        try:
            canonical = vocab.canonical_status("task", status)
            if vocab.is_closed_status("task", canonical):
                # closing banks the clock first - a done task never accrues
                self.focus.stop(user_uuid, task_id)
            task = self.store.update_task(user_uuid, task_id, status=canonical)
        except ValueError as e:
            return _error(str(e), status=404 if "not found" in str(e) else 400)
        return web.json_response({"ok": True, "task": _task_row(task)})


def _client_ip(request: web.Request) -> str:
    """the real client, for rate-limit keying. behind cloudflared every
    connection is loopback; cloudflare stamps the true origin ip in
    CF-Connecting-IP. direct localhost use has no proxy headers and keys on
    the socket peer, which is exactly right there."""
    return (request.headers.get("CF-Connecting-IP")
            or request.remote or "unknown")


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _json_body(request: web.Request):
    try:
        return await request.json()
    except Exception:
        return None
