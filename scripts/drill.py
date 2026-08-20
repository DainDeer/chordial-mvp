#!/usr/bin/env python
"""the drill (phase 7c): load against a scratch rig, numbers for the operator.

simulates N users doing everything a real fleet does - linking devices,
pushing outbox batches, holding websockets, taking conversational turns -
against a REAL WebService over real TCP, and reports client-observed
latencies (p50/p95/p99) plus correctness checks (ACK cursors, websocket
delivery, the sync quota's promises). chat runs through a stub that does
everything except call a model: real EventLog writes, real AppInterface
fan-out, zero tokens.

usage (from the repo root, venv active):

    python scripts/drill.py run                    # 25 users, defaults
    python scripts/drill.py run --users 50 --turns 5
    python scripts/drill.py run --db-url postgresql://localhost/chordial_drill

safety: the rig REFUSES any database url that doesn't contain "drill".
by default it creates its own throwaway sqlite file in a temp dir and
deletes it afterwards (--keep to inspect). it never reads .env state
beyond what it explicitly sets, and never touches a real deployment.

`serve` is the rig's own server half - `run` spawns it as a subprocess;
you only invoke it yourself to drive a rig across two machines.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid as uuid_mod
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

READY_PROBE_SECONDS = 30
WS_REPLY_TIMEOUT = 15


def _require_scratch(url: str) -> None:
    """the whole safety story: this rig only ever speaks to a database
    that announces itself as the drill's. no marker, no run."""
    if "drill" not in url:
        raise SystemExit(
            f"refusing DATABASE_URL {url!r} - the drill only runs against "
            "a database whose url contains 'drill'")


def _rig_env(db_url: str, cap: int, attempts: int) -> dict:
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": db_url,
        "ENABLE_DISCORD": "false",
        "ENABLE_TELEGRAM": "false",
        "SYNC_DAILY_EVENT_CAP": str(cap),
        # the drill measures the endpoints, not the redemption limiter -
        # every simulated device shares 127.0.0.1, so the stock 10/300s
        # would 429 the 11th link. (that same shape is why the knob exists
        # for real many-devices-one-NAT deployments - see config.py.)
        "LINK_RATE_ATTEMPTS": str(attempts),
    })
    return env


# --- serve: the rig's server half ---------------------------------------------


def serve(args: argparse.Namespace) -> None:
    _require_scratch(os.environ.get("DATABASE_URL", ""))
    # imports live here so DATABASE_URL is set before config loads
    from aiohttp import web

    from src.managers.event_log import EventLog
    from src.managers.user_manager import UserManager
    from src.providers.platforms.app import AppInterface
    from src.web.server import WebService

    class DrillChat:
        """the chat seam minus the model: records the turn in the real
        EventLog and delivers one line through the real AppInterface, so
        receipts, fan-out, and delivery confirmation all exercise."""

        def __init__(self, app_interface):
            self.user_manager = UserManager()
            self.app_interface = app_interface

        async def process_message(self, unified):
            log = EventLog(unified.platform_user_id)
            await asyncio.to_thread(
                log.append_message, "user", "user", unified.content,
                platform="app")
            text = f"heard: {unified.content}"
            ok = await self.app_interface.send_message(
                unified.platform_user_id, text, speaker="pip")
            if not ok:
                return "the council stepped out - try again in a moment"
            await asyncio.to_thread(
                log.append_message, "agent", "pip", text, platform="app")
            return None

    async def _serve() -> None:
        app_interface = AppInterface()
        service = WebService(
            user_resolver=lambda: "drill-has-no-web-user",
            chat_service=DrillChat(app_interface),
            app_interface=app_interface)
        runner = web.AppRunner(service.build_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", args.port)
        await site.start()
        print(f"drill serve ready on 127.0.0.1:{args.port}", flush=True)
        await asyncio.Event().wait()

    asyncio.run(_serve())


# --- measurement --------------------------------------------------------------


class Meter:
    def __init__(self, name: str):
        self.name = name
        self.samples: list[float] = []
        self.errors: list[str] = []
        self._started = self._finished = 0.0

    def start(self) -> None:
        self._started = time.monotonic()

    def finish(self) -> None:
        self._finished = time.monotonic()

    def ok(self, elapsed: float) -> None:
        self.samples.append(elapsed)

    def fail(self, detail: str) -> None:
        self.error_count = getattr(self, "error_count", 0) + 1
        if len(self.errors) < 10:
            self.errors.append(detail)

    def row(self) -> str:
        n, errs = len(self.samples), getattr(self, "error_count", 0)
        if not self.samples:
            return f"  {self.name:<18} {n:>6}  {errs:>4}  " + "-" * 34
        ms = sorted(s * 1000 for s in self.samples)

        def pct(p: float) -> float:
            return ms[min(len(ms) - 1, int(p * len(ms)))]

        wall = max(self._finished - self._started, 1e-9)
        return (f"  {self.name:<18} {n:>6}  {errs:>4}  "
                f"{pct(0.50):>7.1f} {pct(0.95):>7.1f} {pct(0.99):>7.1f} "
                f"{ms[-1]:>7.1f}  {n / wall:>7.1f}/s")


async def _timed(meter: Meter, coro, expect: int, label: str):
    t0 = time.monotonic()
    try:
        async with coro as resp:
            body = await resp.json(content_type=None)
            if resp.status != expect:
                meter.fail(f"{label}: {resp.status} {body}")
                return None
            meter.ok(time.monotonic() - t0)
            return body
    except Exception as e:  # connection-level failure is a result too
        meter.fail(f"{label}: {type(e).__name__}: {e}")
        return None


# --- run: the drill itself ----------------------------------------------------


def run(args: argparse.Namespace) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="chordial-drill-"))
    db_url = args.db_url or f"sqlite:///{workdir}/drill.db"
    _require_scratch(db_url)
    soak_per_user = args.batches * args.batch_size
    if soak_per_user > args.cap:
        raise SystemExit(
            f"soak volume per user ({soak_per_user}) exceeds --cap "
            f"({args.cap}) - the soak would trip the quota it isn't probing")

    # rig env in THIS process too, before any src import: seeding and
    # link-code minting go straight to the scratch db
    link_attempts = args.users * 4 + 40
    os.environ.update(_rig_env(db_url, args.cap, link_attempts))

    from src.database import database as db_mod
    from src.database.models import Base, User
    from src.web import device_auth

    Base.metadata.create_all(bind=db_mod.engine)
    user_uuids = [f"drill-user-{i:03d}" for i in range(args.users)]
    probe_uuid = "drill-user-cap-probe"
    with db_mod.SessionLocal() as s:
        for u in [*user_uuids, probe_uuid]:
            s.add(User(uuid=u, preferred_name=u, timezone="UTC",
                       is_test=True))
        s.commit()
    codes = {u: device_auth.mint_device_link_code(u)
             for u in [*user_uuids, probe_uuid]}

    server = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "drill.py"), "serve",
         "--port", str(args.port)],
        env=_rig_env(db_url, args.cap, link_attempts),
        stdout=open(workdir / "server.log", "w"),
        stderr=subprocess.STDOUT, cwd=REPO)
    base = f"http://127.0.0.1:{args.port}"
    failures: list[str] = []
    try:
        asyncio.run(_drill(args, base, user_uuids, probe_uuid, codes,
                           failures))
    finally:
        server.terminate()
        server.wait(timeout=10)
        if args.keep:
            print(f"\nkept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print(f"\nFAILED - {len(failures)} correctness failure(s):")
        for f in failures[:20]:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nall correctness checks passed")


async def _drill(args, base, user_uuids, probe_uuid, codes, failures):
    import aiohttp

    async def wait_ready(session):
        deadline = time.monotonic() + READY_PROBE_SECONDS
        while time.monotonic() < deadline:
            try:
                async with session.get(f"{base}/api/v1/sync/cursor") as r:
                    if r.status == 401:  # up, and honestly refusing
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.2)
        raise SystemExit("the serve subprocess never came up - see server.log")

    conn = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=conn) as session:
        await wait_ready(session)
        print(f"\nthe drill: {args.users} users x ({args.batches} batches of "
              f"{args.batch_size} events, {args.turns} turns), cap {args.cap}"
              f"\nserver {base}, db {'postgres' if 'postgres' in (args.db_url or '') else 'sqlite (wal)'}\n")

        meters = []

        # --- phase 1: the link storm --------------------------------------
        link = Meter("link storm")
        meters.append(link)
        tokens: dict[str, str] = {}

        async def do_link(u):
            body = await _timed(
                link, session.post(f"{base}/api/v1/devices/link",
                                   json={"code": codes[u],
                                         "name": f"drill-{u}"}),
                201, f"link {u}")
            if body:
                tokens[u] = body["token"]

        link.start()
        await asyncio.gather(*(do_link(u) for u in [*user_uuids, probe_uuid]))
        link.finish()
        missing = [u for u in user_uuids if u not in tokens]
        if missing:
            failures.append(f"{len(missing)} devices failed to link - "
                            "later phases skipped them")

        def hdr(u):
            return {"Authorization": f"Bearer {tokens[u]}"}

        # --- phase 2: the sync soak ---------------------------------------
        soak = Meter("sync push")
        cursor_m = Meter("cursor read")
        meters += [soak, cursor_m]
        event_types = ["session.started", "focus_block.started",
                       "drift.detected", "return.detected",
                       "focus_block.completed", "session.ended"]

        def batch(seq0, n):
            events = []
            for i in range(n):
                etype = event_types[(seq0 + i) % len(event_types)]
                payload = ({"label": "drill block", "run_seconds": 1500,
                            "banked_seconds_today": 3000}
                           if etype.startswith("focus_block") else {})
                events.append({"id": str(uuid_mod.uuid4()),
                               "seq": seq0 + i, "type": etype,
                               "payload": payload})
            return events

        async def do_soak(u):
            for b in range(args.batches):
                body = await _timed(
                    soak, session.post(
                        f"{base}/api/v1/sync/events", headers=hdr(u),
                        json={"events": batch(b * args.batch_size + 1,
                                              args.batch_size)}),
                    200, f"soak {u} b{b}")
                if body and body.get("rejected"):
                    failures.append(f"soak {u}: rejected events "
                                    f"{body['rejected'][:2]}")

        soak.start()
        await asyncio.gather(*(do_soak(u) for u in user_uuids
                               if u in tokens))
        soak.finish()

        async def check_cursor(u):
            body = await _timed(
                cursor_m, session.get(f"{base}/api/v1/sync/cursor",
                                      headers=hdr(u)),
                200, f"cursor {u}")
            want = args.batches * args.batch_size
            if body and body.get("acked_seq") != want:
                failures.append(f"cursor {u}: acked {body.get('acked_seq')}"
                                f" != pushed {want}")

        cursor_m.start()
        await asyncio.gather(*(check_cursor(u) for u in user_uuids
                               if u in tokens))
        cursor_m.finish()

        # --- phase 3: the websocket hold ----------------------------------
        ws_connect = Meter("ws connect")
        meters.append(ws_connect)
        sockets, listeners = [], []
        waiting: dict[str, tuple[float, asyncio.Future]] = {}

        async def listen(ws):
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                frame = json.loads(msg.data)
                if frame.get("type") != "message":
                    continue
                for marker, (t0, fut) in list(waiting.items()):
                    if marker in frame.get("content", "") and not fut.done():
                        fut.set_result(time.monotonic() - t0)

        ws_connect.start()
        for u in user_uuids:
            if u not in tokens:
                continue
            t0 = time.monotonic()
            try:
                ws = await session.ws_connect(f"{base}/api/v1/ws",
                                              heartbeat=30)
                await ws.send_json({"token": tokens[u]})
                hello = json.loads((await ws.receive(timeout=10)).data)
                if hello.get("type") != "hello":
                    raise RuntimeError(f"expected hello, got {hello}")
                ws_connect.ok(time.monotonic() - t0)
                sockets.append(ws)
                listeners.append(asyncio.create_task(listen(ws)))
            except Exception as e:
                ws_connect.fail(f"ws {u}: {type(e).__name__}: {e}")
        ws_connect.finish()
        if len(sockets) != len(tokens) - 1:  # probe user holds no socket
            failures.append(f"only {len(sockets)} of {len(tokens) - 1} "
                            "websockets connected")

        # --- phase 4: the turn storm --------------------------------------
        turn = Meter("turn POST")
        ws_deliver = Meter("ws delivery")
        meters += [turn, ws_deliver]

        async def do_turns(u):
            for _ in range(args.turns):
                marker = uuid_mod.uuid4().hex
                fut = asyncio.get_running_loop().create_future()
                waiting[marker] = (time.monotonic(), fut)
                body = await _timed(
                    turn, session.post(
                        f"{base}/api/v1/rooms/current/messages",
                        headers=hdr(u),
                        json={"text": f"drill turn {marker}",
                              "client_message_id": str(uuid_mod.uuid4())}),
                    200, f"turn {u}")
                if body and not any(marker in m.get("content", "")
                                    for m in body.get("messages", ())):
                    failures.append(f"turn {u}: reply missing from POST body")
                try:
                    ws_deliver.ok(await asyncio.wait_for(
                        fut, timeout=WS_REPLY_TIMEOUT))
                except asyncio.TimeoutError:
                    ws_deliver.fail(f"turn {u}: no ws delivery within "
                                    f"{WS_REPLY_TIMEOUT}s")
                finally:
                    waiting.pop(marker, None)

        turn.start()
        ws_deliver.start()
        await asyncio.gather(*(do_turns(u) for u in user_uuids
                               if u in tokens))
        turn.finish()
        ws_deliver.finish()

        # --- phase 5: the cap probe ---------------------------------------
        # one fresh user marches straight into the quota: expect clean 200s
        # up to the cap, an honest 429 past it, and - the documented
        # promise - a RETRY of an already-acked batch still acks AT the cap
        probe = Meter("cap probe")
        meters.append(probe)
        probe.start()
        if probe_uuid in tokens:
            first_batch = batch(1, args.batch_size)
            saw_429 = False
            applied_total = 0
            for b in range(args.cap // args.batch_size + 2):
                events = (first_batch if b == 0
                          else batch(b * args.batch_size + 1,
                                     args.batch_size))
                t0 = time.monotonic()
                async with session.post(f"{base}/api/v1/sync/events",
                                        headers=hdr(probe_uuid),
                                        json={"events": events}) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status == 200:
                        probe.ok(time.monotonic() - t0)
                        applied_total += body.get("applied", 0)
                    elif resp.status == 429:
                        probe.ok(time.monotonic() - t0)
                        saw_429 = True
                        if "daily event cap" not in body.get("error", ""):
                            failures.append(f"cap 429 copy wrong: {body}")
                        break
                    else:
                        probe.fail(f"cap probe b{b}: {resp.status} {body}")
            if not saw_429:
                failures.append("cap probe never hit 429")
            if applied_total != args.cap:
                failures.append(f"cap probe applied {applied_total} "
                                f"!= cap {args.cap}")
            async with session.post(f"{base}/api/v1/sync/events",
                                    headers=hdr(probe_uuid),
                                    json={"events": first_batch}) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200 or body.get("duplicates") != \
                        args.batch_size:
                    failures.append(
                        f"retry-at-cap broke dedup-before-quota: "
                        f"{resp.status} {body}")
        probe.finish()

        for ws in sockets:
            await ws.close()
        for task in listeners:
            task.cancel()

        print(f"  {'phase':<18} {'ops':>6}  {'errs':>4}  "
              f"{'p50ms':>7} {'p95ms':>7} {'p99ms':>7} {'maxms':>7}  "
              f"{'rate':>9}")
        for m in meters:
            print(m.row())
            for e in m.errors[:3]:
                print(f"      ! {e}")
            failures.extend(m.errors)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="spawn a scratch rig and drill it")
    p_run.add_argument("--users", type=int, default=25)
    p_run.add_argument("--batches", type=int, default=3,
                       help="sync batches per device")
    p_run.add_argument("--batch-size", type=int, default=100,
                       help="events per batch (sidecar pushes 100)")
    p_run.add_argument("--turns", type=int, default=3,
                       help="conversational turns per user")
    p_run.add_argument("--cap", type=int, default=1000,
                       help="SYNC_DAILY_EVENT_CAP for the rig")
    p_run.add_argument("--db-url", default=None,
                       help="scratch db (must contain 'drill'); default: "
                            "throwaway sqlite")
    p_run.add_argument("--port", type=int, default=8710)
    p_run.add_argument("--keep", action="store_true",
                       help="keep the workdir (db + server.log)")
    p_serve = sub.add_parser("serve", help="the rig's server half")
    p_serve.add_argument("--port", type=int, default=8710)
    args = parser.parse_args()
    if args.cmd == "serve":
        serve(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
