"""the sidecar's loopback api: what the deer window (and the shell) talk to.

binds 127.0.0.1 ONLY - this process serves the person's own machine, never
a network. the browser-facing risk is a random website poking localhost
ports, which the exact-origin CORS allowlist refuses (same posture as the
main server's /api/v1); the tauri shell and the vite dev origin are the
only welcome callers.

surface (all JSON):
    POST /v1/handshake        {token, server_url} - the shell hands the
                              sidecar its device credential once; a NEW
                              device uuid rebases the outbox (fresh server
                              cursor) and un-parks the sync pump
    GET  /v1/state            {session, line}
    POST /v1/session/start    {label?, minutes?} -> state + an authored line
    POST /v1/session/stop     -> state + outcome + line
    WS   /v1/ws               pushes {type: "state"|"line", ...} as they happen

the 1-second ticker settles expired blocks (the ding), so completion lines
arrive over the websocket the moment the clock runs out - no client polling.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Set

from aiohttp import web

from src.sidecar.lines import pick_line
from src.sidecar.sessions import SessionEngine, SessionError
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
                 engine: Optional[SessionEngine] = None,
                 pump: Optional[OutboxPump] = None):
        self.store = store
        self.engine = engine or SessionEngine(store)
        self.pump = pump or OutboxPump(store)
        self._sockets: Set[web.WebSocketResponse] = set()
        self._last_line: Optional[str] = None
        self._tasks: list[asyncio.Task] = []

    # --- app assembly -----------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._cors])
        app.router.add_post("/v1/handshake", self._handshake)
        app.router.add_get("/v1/state", self._state)
        app.router.add_post("/v1/session/start", self._session_start)
        app.router.add_post("/v1/session/stop", self._session_stop)
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

    async def _announce(self, moment: str) -> str:
        line = pick_line(moment, last=self._last_line)
        self._last_line = line
        await self._broadcast({"type": "line", "moment": moment,
                               "text": line})
        await self._broadcast({"type": "state",
                               "session": self.engine.state()})
        return line

    # --- the ticker -----------------------------------------------------------

    async def _ticker(self) -> None:
        """settle expired blocks every second - the ding."""
        while True:
            await asyncio.sleep(1)
            try:
                if self.engine.tick() == "completed":
                    await self._announce("session_complete")
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
            "session": self.engine.state(),
            "line": self._last_line,
            "linked": bool(self.store.get("device_token")),
            "sync_error": self.store.get("sync_error") or None,
        })

    async def _session_start(self, request: web.Request) -> web.Response:
        body = await _json_body(request) or {}
        label = body.get("label") if isinstance(body, dict) else None
        minutes = body.get("minutes", 25) if isinstance(body, dict) else 25
        try:
            state = self.engine.start(
                label if isinstance(label, str) else None, minutes)
        except SessionError as e:
            return _error(str(e))
        line = await self._announce("session_start")
        return web.json_response({"session": state, "line": line})

    async def _session_stop(self, _request: web.Request) -> web.Response:
        outcome = self.engine.stop()
        if outcome is None:
            return _error("no block is running", status=409)
        line = await self._announce(f"session_{outcome}")
        return web.json_response({
            "session": self.engine.state(),
            "outcome": outcome,
            "line": line,
        })

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            await ws.send_json({"type": "state",
                                "session": self.engine.state()})
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
