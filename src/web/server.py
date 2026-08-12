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
import uuid as uuid_mod
from pathlib import Path
from typing import Optional
from weakref import WeakValueDictionary

from aiohttp import web

from config import Config
from src.database.database import get_db
from src.database.models import User
from src.personas import CHAIR_ID
from src.services.workspace import get_store, vocab
from src.services.workspace.agenda import user_today
from src.services.workspace.focus import FocusStore
from src.services import device_sync
from src.utils.timezone_utils import utc_now
from src.web import auth, device_auth, receipts

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# how often an open websocket re-proves its device token. a revoked device's
# socket dies within this window (module-level so tests can shrink it)
WS_REVALIDATE_SECONDS = 60.0


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
                 user_resolver=resolve_web_user, chat_service=None,
                 app_interface=None):
        self.store = store or get_store()
        self.focus = focus or FocusStore()
        self._resolve_user = user_resolver
        # the app-facing chat seam: process_message runs a turn, the app
        # interface's queues carry the delivered lines back. both None on
        # deployments that run the focus view without the chat engine.
        self.chat_service = chat_service
        self.app_interface = app_interface
        # one app-originated turn per user at a time: the reply-capture queue
        # is user-wide, so a second concurrent POST would otherwise drain the
        # first one's lines. weak values, same pattern as ChatService's locks.
        self._send_locks: WeakValueDictionary[str, asyncio.Lock] = \
            WeakValueDictionary()
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
        app = web.Application(
            middlewares=[self._security_headers, self._origin_guard,
                         self._cors_v1])
        app.router.add_get("/", self._index)
        app.router.add_get("/login", self._login_page)
        app.router.add_post("/api/login/redeem", self._api_login_redeem)
        app.router.add_post("/api/logout", self._api_logout)
        app.router.add_get("/api/today", self._api_today)
        app.router.add_post("/api/focus/start", self._api_focus_start)
        app.router.add_post("/api/focus/pause", self._api_focus_pause)
        app.router.add_post("/api/tasks/{task_id}/status", self._api_task_status)
        # --- /api/v1: the app-facing api (docs/ROOMS_DESIGN.md section 10).
        # device-bearer auth on every route; tenant scoping is structural -
        # handlers only ever see the authenticated device's user.
        app.router.add_post("/api/v1/devices/link", self._api_device_link)
        app.router.add_get("/api/v1/devices", self._api_devices_list)
        app.router.add_post("/api/v1/devices/revoke", self._api_device_revoke)
        app.router.add_post("/api/v1/sync/events", self._api_sync_events)
        app.router.add_get("/api/v1/today", self._api_v1_today)
        # rooms v0: the legacy per-user stream presented as today's room.
        # phase 2 makes rooms first-class; these routes keep their shape.
        app.router.add_get("/api/v1/rooms/current", self._api_room_current)
        app.router.add_get("/api/v1/rooms/current/messages",
                           self._api_room_messages)
        app.router.add_post("/api/v1/rooms/current/messages",
                            self._api_room_send)
        app.router.add_get("/api/v1/ws", self._api_ws)
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

    # --- response headers -----------------------------------------------------

    @web.middleware
    async def _security_headers(self, request: web.Request, handler):
        """every response names who may frame it. an unset header lets any
        site iframe the logged-in view and clickjack its buttons; the config
        default allows only ourselves, and deployments widen it deliberately
        (the portfolio desktop embeds focus.exe in a window).

        Cache-Control: no-cache on everything: without it, browsers
        heuristically reuse the html for ~10% of its Last-Modified age (a
        page cached before a deploy masked that deploy for days), and
        cloudflare edge-caches static js/css for hours. no-cache still
        permits storing - Last-Modified revalidation keeps 304s fast - but
        every load asks the origin, so a deploy is visible on next reload."""
        csp = "frame-ancestors " + Config.WEB_FRAME_ANCESTORS
        try:
            response = await handler(request)
        except web.HTTPException as e:
            e.headers["Content-Security-Policy"] = csp
            e.headers.setdefault("Cache-Control", "no-cache")
            raise
        response.headers["Content-Security-Policy"] = csp
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # --- cross-origin writes --------------------------------------------------

    @web.middleware
    async def _origin_guard(self, request: web.Request, handler):
        """public mode refuses state-changing requests from any other origin.
        SameSite=Lax alone doesn't cover a sibling subdomain on the same
        personal domain (the portfolio site could POST here with cookies, and
        aiohttp parses json out of text/plain "simple" requests). browsers
        always send Origin on cross-origin POSTs, so a mismatch is decisive;
        absent means non-browser (curl), which csrf doesn't apply to."""
        if request.path.startswith("/api/v1"):
            # bearer-token routes carry no ambient credentials (no cookies),
            # so csrf doesn't apply; _cors_v1 owns their origin policy
            return await handler(request)
        if Config.web_auth_enabled() and request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin is not None and origin != Config.WEB_PUBLIC_URL:
                return _error("cross-origin request refused", status=403)
        return await handler(request)

    # --- /api/v1 CORS ---------------------------------------------------------

    @web.middleware
    async def _cors_v1(self, request: web.Request, handler):
        """lets the tauri webview (and the vite dev server) call the app api
        from its own origin. scoped to /api/v1 only; the allowlist is exact
        origins from config, never a wildcard. non-browser clients (no
        Origin header) pass straight through - the api's real gate is the
        bearer token, this just satisfies the browser's preflight."""
        if not request.path.startswith("/api/v1"):
            return await handler(request)
        origin = request.headers.get("Origin")
        allowed = origin in Config.APP_ALLOWED_ORIGINS if origin else False

        def _stamp(headers) -> None:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Headers"] = \
                "Authorization, Content-Type"
            headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            headers["Access-Control-Max-Age"] = "600"

        if request.method == "OPTIONS":
            # the preflight; no route handlers register OPTIONS
            if not allowed:
                return _error("origin not allowed", status=403)
            response = web.Response(status=204)
            _stamp(response.headers)
            return response
        try:
            response = await handler(request)
        except web.HTTPException as e:
            if allowed:
                _stamp(e.headers)
            raise
        if allowed:
            _stamp(response.headers)
        return response

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
            "break_minutes": Config.BREAK_MINUTES,
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


    # --- /api/v1: devices & sync ---------------------------------------------

    async def _device(self, request: web.Request) -> device_auth.DeviceIdentity:
        """the device this request acts as, or 401. handlers await it first -
        it IS the tenant boundary (user_uuid only ever comes from here)."""
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else None
        identity = await asyncio.to_thread(
            device_auth.verify_device_token, token)
        if identity is None:
            raise web.HTTPUnauthorized(
                text='{"error": "invalid or revoked device token"}',
                content_type="application/json")
        return identity

    async def _api_device_link(self, request: web.Request) -> web.Response:
        if not self._limiter.allow(_client_ip(request)):
            return _error("too many attempts - wait a few minutes", status=429)
        body = await _json_body(request)
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            return _error("code (string) required")
        name = body.get("name") if isinstance(body, dict) else None
        result = await asyncio.to_thread(
            device_auth.link_device, code,
            name if isinstance(name, str) and name else "device")
        if result is None:
            return _error("that code is invalid or expired - ask chordial "
                          "for a fresh one", status=401)
        device_uuid, token = result
        return web.json_response(
            {"device_id": device_uuid, "token": token}, status=201)

    async def _api_devices_list(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        devices = await asyncio.to_thread(
            device_auth.list_devices, identity.user_uuid)
        for d in devices:
            d["current"] = d["device_id"] == identity.device_uuid
        return web.json_response({"devices": devices})

    async def _api_device_revoke(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        body = await _json_body(request)
        target = body.get("device_id") if isinstance(body, dict) else None
        if not isinstance(target, str):
            return _error("device_id (string) required")
        revoked = await asyncio.to_thread(
            device_auth.revoke_device, identity.user_uuid, target)
        if not revoked:
            return _error("no such active device", status=404)
        return web.json_response({"ok": True})

    async def _api_sync_events(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        body = await _json_body(request)
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list):
            return _error("events (array) required")
        try:
            result = await asyncio.to_thread(
                device_sync.apply_events, identity.id, events)
        except device_sync.QuotaExceeded as e:
            return _error(str(e), status=429)
        except ValueError as e:
            return _error(str(e))
        return web.json_response(result.as_dict())

    async def _api_v1_today(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        return await asyncio.to_thread(self._today_payload, identity.user_uuid)

    # --- /api/v1: rooms (v0 - the legacy stream as today's room) --------------

    async def _api_room_current(self, request: web.Request) -> web.Response:
        identity = await self._device(request)

        def build() -> dict:
            from src.services.rooms import get_room_store
            from src.services.workspace.agenda import user_today
            room = get_room_store().current_room(
                identity.user_uuid, user_today(identity.user_uuid))
            return {
                "room": {
                    "id": room["room_uuid"],
                    "type": room["room_type"],
                    "date": room["date"],
                    "status": room["status"],
                },
                "chat_available": self.chat_service is not None,
            }
        return web.json_response(await asyncio.to_thread(build))

    async def _api_room_messages(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 200))
        except ValueError:
            return _error("limit must be an integer")

        def read() -> list[dict]:
            from src.database.models import ConversationEvent
            from src.services.rooms import get_room_store
            from src.services.workspace.agenda import user_today
            room = get_room_store().current_room(
                identity.user_uuid, user_today(identity.user_uuid))
            with get_db() as db:
                rows = db.query(ConversationEvent).filter(
                    ConversationEvent.stream_id == room["room_uuid"],
                    ConversationEvent.kind == "message",
                ).order_by(ConversationEvent.id.desc()).limit(limit).all()
                # serialize INSIDE the session - orm rows detach at close
                return [{
                    "id": r.id,
                    "author": r.author,
                    "author_type": r.author_type,
                    "content": r.content,
                    "at": r.created_at.isoformat() if r.created_at else None,
                    "platform": r.platform,
                } for r in reversed(rows)]
        return web.json_response({"messages": await asyncio.to_thread(read)})

    async def _api_room_send(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        if self.chat_service is None or self.app_interface is None:
            return _error("chat is not available on this deployment",
                          status=503)
        body = await _json_body(request)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return _error("text (non-empty string) required")
        if len(text) > 4000:
            return _error("text too long (max 4000 characters)")
        client_message_id = (body.get("client_message_id")
                             if isinstance(body, dict) else None)
        if not isinstance(client_message_id, str):
            return _error("client_message_id (uuid string) required - it is "
                          "the retry-safety key for this send")
        try:
            uuid_mod.UUID(client_message_id)
        except ValueError:
            return _error("client_message_id must be a uuid")
        client_message_id = client_message_id.lower()
        user_uuid = identity.user_uuid

        # idempotency: claim before the model runs. a retry after a lost
        # response replays the stored lines - it never costs a second turn
        # (or duplicate tool mutations). same contract shape as sync events.
        outcome = await asyncio.to_thread(
            receipts.claim, identity.id, user_uuid, client_message_id)
        if outcome.status == "replay":
            return web.json_response(
                {"ok": True, "messages": outcome.response, "replayed": True})
        if outcome.status == "in_flight":
            return _error("this message is already being processed - "
                          "retry in a moment", status=409)

        # bind the 'app' platform identity to THIS user before the turn runs.
        # the app's platform_user_id IS the user_uuid (the device token
        # already proved who they are), so get_or_create_user can never
        # mint a stranger - but the identity row must exist for delivery
        # targeting and active_platform() to mean anything.
        link = await self.chat_service.user_manager.link_platform_identity(
            user_uuid, "app", user_uuid)
        if link == "conflict":     # structurally impossible; fail loudly
            await asyncio.to_thread(
                receipts.release, identity.id, client_message_id)
            return _error("app identity conflict", status=500)

        from src.models.unified_message import UnifiedMessage
        lock = self._send_locks.get(user_uuid)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[user_uuid] = lock
        try:
            async with lock:
                queue = self.app_interface.subscribe(user_uuid)
                try:
                    fallback = await self.chat_service.process_message(
                        UnifiedMessage(
                            content=text.strip(),
                            platform_user_id=user_uuid,
                            platform="app",
                            platform_message_id=client_message_id,
                            metadata={"device": identity.device_uuid},
                            # the app room is the COUNCIL's room (phase 3b):
                            # a shared scope, so the routing spine picks the
                            # speaker instead of hard-wiring the chair. the
                            # 'group' id is the user - the app fans delivery
                            # to their own connected surfaces.
                            chat_scope="group",
                            group_chat_id=user_uuid,
                        ))
                    replies = self.app_interface.drain(queue)
                finally:
                    self.app_interface.unsubscribe(user_uuid, queue)
        except Exception:
            # no response exists to replay; free the claim for honest retries
            await asyncio.to_thread(
                receipts.release, identity.id, client_message_id)
            raise

        if fallback and not replies:
            # refusal/error copy: shown, never persisted (matches platforms).
            # deliberately NOT stored in the receipt either - a retry of an
            # errored turn should get a fresh chance, not a replayed apology.
            await asyncio.to_thread(
                receipts.release, identity.id, client_message_id)
            replies = [{"type": "message", "author": CHAIR_ID,
                        "content": fallback, "ephemeral": True}]
        else:
            await asyncio.to_thread(
                receipts.store, identity.id, client_message_id, replies)
        return web.json_response({"ok": True, "messages": replies})

    # --- /api/v1: the websocket ----------------------------------------------

    async def _api_ws(self, request: web.Request) -> web.WebSocketResponse:
        """the app's live channel. auth is first-message ({"token": ...}
        within 10s) because webviews can't set headers on websocket
        upgrades. after auth the socket receives every line delivered to
        this user through the app interface (one socket, whole council -
        attribution rides in the payload)."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        if self.app_interface is None:
            await ws.close(code=4503, message=b"chat not available")
            return ws
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except Exception:
            await ws.close(code=4401, message=b"auth required")
            return ws
        token = first.get("token") if isinstance(first, dict) else None
        identity = await asyncio.to_thread(
            device_auth.verify_device_token, token)
        if identity is None:
            await ws.close(code=4401, message=b"invalid device token")
            return ws

        user_uuid = identity.user_uuid
        queue = self.app_interface.subscribe(user_uuid)
        await ws.send_json({"type": "hello", "device": identity.device_uuid})

        async def pump():
            """forward deliveries, revalidating the device on a cadence so a
            revoked device's socket dies within the window instead of
            listening to the user's council forever."""
            next_check = asyncio.get_event_loop().time() + WS_REVALIDATE_SECONDS
            while True:
                remaining = next_check - asyncio.get_event_loop().time()
                if remaining <= 0:
                    still = await asyncio.to_thread(
                        device_auth.verify_device_token, token)
                    if still is None:
                        await ws.close(code=4401, message=b"device revoked")
                        return
                    next_check = (asyncio.get_event_loop().time()
                                  + WS_REVALIDATE_SECONDS)
                    continue
                try:
                    payload = await asyncio.wait_for(queue.get(),
                                                     timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                await ws.send_json(payload)

        pump_task = asyncio.create_task(pump())
        try:
            # reading keeps the connection's close/ping handling alive;
            # inbound chat rides POST, so client frames are ignored
            async for _ in ws:
                pass
        finally:
            pump_task.cancel()
            self.app_interface.unsubscribe(user_uuid, queue)
        return ws


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
