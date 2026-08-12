"""the sidecar's loopback api: what the deer window (and the shell) talk to.

binds 127.0.0.1 ONLY - this process serves the person's own machine, never
a network. the browser-facing risk is a random website poking localhost
ports, which the exact-origin CORS allowlist refuses (same posture as the
main server's /api/v1); the tauri shell and the vite dev origin are the
only welcome callers.

surface (all JSON):
    POST /v1/handshake      {token, server_url} - the shell hands the
                            sidecar its device credential once; a NEW
                            device uuid rebases the outbox (fresh server
                            cursor) and un-parks the sync pump
    GET  /v1/state          {focus, line, linked, sync_error}
    POST /v1/focus/start    {task_id?, label?, target_minutes?} - start or
                            SWITCH (a running clock on another task pauses
                            and banks first)
    POST /v1/focus/pause    stop the clock, bank the run
    POST /v1/focus/finish   the task is DONE - the celebration moment; the
                            window marks the task done on the chordial
                            server itself (tasks are canonical there)
    WS   /v1/ws             pushes {type: "state"|"line", ...} as they happen

the 1-second ticker announces a run crossing its block target ONCE - a
gentle ding that ends nothing; the clock keeps counting overtime until the
person lands it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Set

from aiohttp import web

from src.sidecar.drift import ActivityState, DriftWatch
from src.sidecar.focus import FocusEngine, FocusError
from src.sidecar.gates import SpeechGates
from src.sidecar.lines import pick_line
from src.sidecar.store import SidecarStore
from src.sidecar.sync import OutboxPump

logger = logging.getLogger(__name__)

SIDECAR_HOST = "127.0.0.1"
SIDECAR_PORT = int(os.getenv("SIDECAR_PORT", "8485"))
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "SIDECAR_ALLOWED_ORIGINS",
        "tauri://localhost,http://localhost:1420").split(",") if o.strip()
]


class SidecarService:
    def __init__(self, store: SidecarStore,
                 engine: Optional[FocusEngine] = None,
                 pump: Optional[OutboxPump] = None,
                 activity: Optional[ActivityState] = None,
                 drift: Optional[DriftWatch] = None,
                 gates: Optional[SpeechGates] = None):
        self.store = store
        self.engine = engine or FocusEngine(store)
        self.pump = pump or OutboxPump(store)
        self.activity = activity or ActivityState()
        self.drift = drift or DriftWatch(store, self.activity)
        self.gates = gates or SpeechGates()
        self._sockets: Set[web.WebSocketResponse] = set()
        self._last_line: Optional[str] = None
        self._tasks: list[asyncio.Task] = []

    # --- app assembly -----------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._cors])
        app.router.add_post("/v1/handshake", self._handshake)
        app.router.add_get("/v1/state", self._state)
        app.router.add_post("/v1/focus/start", self._focus_start)
        app.router.add_post("/v1/focus/pause", self._focus_pause)
        app.router.add_post("/v1/focus/finish", self._focus_finish)
        app.router.add_post("/v1/activity", self._activity)
        app.router.add_get("/v1/ws", self._ws)
        app.on_startup.append(self._start_background)
        app.on_cleanup.append(self._stop_background)
        return app

    async def _start_background(self, _app) -> None:
        self._tasks = [
            asyncio.create_task(self._ticker()),
            asyncio.create_task(self.pump.run()),
        ]

    async def _stop_background(self, _app) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.pump.close()

    # --- cors ------------------------------------------------------------------

    @web.middleware
    async def _cors(self, request: web.Request, handler):
        origin = request.headers.get("Origin")
        allowed = origin in ALLOWED_ORIGINS if origin else False

        def _stamp(headers) -> None:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Headers"] = "Content-Type"
            headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            headers["Access-Control-Max-Age"] = "600"

        if request.method == "OPTIONS":
            response = web.Response(status=204)
            if allowed:
                _stamp(response.headers)
            return response
        if origin and not allowed:
            return web.json_response({"error": "origin not allowed"},
                                     status=403)
        response = await handler(request)
        if allowed:
            _stamp(response.headers)
        return response

    # --- broadcast ---------------------------------------------------------------

    async def _broadcast(self, payload: dict) -> None:
        dead = []
        for ws in self._sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)

    def _world(self) -> dict:
        """the state payload: the clock plus derived activity flags (never
        the bundle id - the ui gets moods, not surveillance)."""
        return {
            "type": "state",
            "focus": self.engine.state(),
            "activity": {**self.activity.snapshot(),
                         "drifting": self.drift.drifting},
        }

    async def _announce(self, moment: str) -> str:
        line = pick_line(moment, last=self._last_line)
        self._last_line = line
        await self._broadcast({"type": "line", "moment": moment,
                               "text": line})
        await self._broadcast(self._world())
        return line

    async def _announce_unprompted(self, moment: str) -> None:
        """spontaneous speech rides the gate stack: hushed in meetings,
        silent in quiet hours, capped per rolling hour. the state still
        broadcasts either way - the ui may move even when the deer holds
        her tongue."""
        if self.gates.allow(blocked=self.activity.blocked):
            self.gates.spend()
            await self._announce(moment)
        else:
            await self._broadcast(self._world())

    # --- the ticker -----------------------------------------------------------

    async def _ticker(self) -> None:
        """the second-hand: the target ding and the drift watch, both
        unprompted and therefore both gated."""
        while True:
            await asyncio.sleep(1)
            try:
                if self.engine.crossed_target():
                    await self._announce_unprompted("block_target")
                moment = self.drift.tick(
                    self.engine.state().get("running", False))
                if moment:
                    await self._announce_unprompted(moment)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ticker hiccup; continuing")

    # --- handlers ------------------------------------------------------------

    async def _handshake(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        token = body.get("token") if isinstance(body, dict) else None
        server_url = body.get("server_url") if isinstance(body, dict) else None
        if not isinstance(token, str) or "." not in token:
            return _error("token (device bearer token) required")
        if not isinstance(server_url, str) or not server_url.startswith("http"):
            return _error("server_url (http origin) required")

        device_uuid = token.partition(".")[0]
        previous = self.store.get("device_uuid")
        if previous and previous != device_uuid:
            moved = self.store.rebase_outbox()
            logger.info("new device credential: outbox rebased (%d pending)",
                        moved)
        self.store.put("device_uuid", device_uuid)
        self.store.put("device_token", token)
        self.store.put("server_url", server_url.rstrip("/"))
        self.store.put("sync_error", "")     # un-park the pump
        return web.json_response({"ok": True})

    async def _state(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "focus": self.engine.state(),
            "activity": {**self.activity.snapshot(),
                         "drifting": self.drift.drifting},
            "line": self._last_line,
            "linked": bool(self.store.get("device_token")),
            "sync_error": self.store.get("sync_error") or None,
        })

    async def _activity(self, request: web.Request) -> web.Response:
        """the collector's 2s heartbeat. the rust boundary already
        sanitized; this re-checks shape because loopback posts can come
        from anything on the machine."""
        body = await _json_body(request)
        if not isinstance(body, dict):
            return _error("json object required")
        bundle_id = body.get("bundle_id")
        if bundle_id is not None and not (
                isinstance(bundle_id, str) and 0 < len(bundle_id) <= 200
                and all(c.isalnum() or c in ".-_" for c in bundle_id)):
            bundle_id = None            # malformed = absent, never trusted
        idle = body.get("idle_seconds")
        if not isinstance(idle, (int, float)) or isinstance(idle, bool) \
                or idle < 0:
            return _error("idle_seconds (non-negative number) required")
        was_blocked = self.activity.blocked
        self.activity.observe(bundle_id, float(idle))
        if self.activity.blocked != was_blocked:
            # hush on/off changes the deer's whole posture, and it happens
            # without a line - push the state so the window follows
            await self._broadcast(self._world())
        return web.json_response({"ok": True})

    async def _focus_start(self, request: web.Request) -> web.Response:
        body = await _json_body(request) or {}
        task_id = body.get("task_id") if isinstance(body, dict) else None
        label = body.get("label") if isinstance(body, dict) else None
        target = body.get("target_minutes", 25) if isinstance(body, dict) \
            else 25
        switching = self.engine.state().get("running", False)
        try:
            state = self.engine.start(
                task_id if isinstance(task_id, int) else None,
                label if isinstance(label, str) else None,
                target)
        except FocusError as e:
            return _error(str(e))
        line = await self._announce(
            "focus_switch" if switching else "focus_start")
        return web.json_response({"focus": state, "line": line})

    async def _focus_pause(self, _request: web.Request) -> web.Response:
        run = self.engine.pause()
        if run is None:
            return _error("no clock is running", status=409)
        line = await self._announce("focus_pause")
        return web.json_response({
            "focus": self.engine.state(),
            "run": run,
            "line": line,
        })

    async def _focus_finish(self, _request: web.Request) -> web.Response:
        try:
            run = self.engine.finish()
        except FocusError as e:
            return _error(str(e), status=409)
        line = await self._announce("task_finished")
        return web.json_response({
            "focus": self.engine.state(),
            "run": run,
            "line": line,
        })

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            await ws.send_json(self._world())
            async for _ in ws:
                pass          # loopback push channel; client frames ignored
        finally:
            self._sockets.discard(ws)
        return ws


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _json_body(request: web.Request):
    try:
        return await request.json()
    except Exception:
        return None
