"""the desktop app as a delivery platform (docs/ROOMS_DESIGN.md section 10).

registered with the MessageRouter as platform 'app', exactly like discord or
telegram - the orchestrator's confirmed-delivery-before-recording invariant
holds unchanged. delivery lands in in-process queues: every websocket
connection and every in-flight POST /rooms/current/messages request
subscribes a queue; send_message fans a payload out to all of a user's
subscribers and confirms if at least one was listening. no subscriber means
undelivered (False) - a transient absence, never an UndeliverableError, so
the router never deactivates the app link just because the app is closed.

speaker attribution rides IN the payload (single websocket, whole council -
the same one-bot decision as the tether), which is why the router forwards
`speaker=` to send_message.

this interface has no background loop: it is router-registered but NOT
supervised (nothing to run), and inbound messages arrive via the http/ws
handlers in src/web/server.py rather than handle_incoming_message.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional, Set, Tuple

from config import Config
from src.personas import CHAIR_ID
from src.providers.platforms.base import BaseInterface
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# a slow websocket consumer must never block delivery confirmation; a queue
# that somehow backs up this far is a dead consumer wearing a live socket
_QUEUE_CAP = 256

# the desktop heartbeats presence every ~30s over its websocket; a live
# socket whose reports have gone this quiet means the reporter died, and an
# unreporting machine cannot prove anyone is at it - count as away. (a
# protocol cadence detail, deliberately not Config: the idle THRESHOLD is
# the user-meaningful knob, Config.PRESENCE_IDLE_SECONDS.)
_PRESENCE_STALE_SECONDS = 90.0


class AppInterface(BaseInterface):
    """fan-out delivery to the user's connected app surfaces - and, since
    those surfaces are the only ones that can prove someone is at the desk,
    the source of truth for presence-aware proactive routing (section 9):
    the websocket handler feeds note_presence from the app's heartbeats, and
    presence_state answers 'deer bubble or tether?' for the pulse."""

    platform = "app"

    def __init__(self, chat_service=None):
        super().__init__(chat_service)
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        # user_uuid -> (monotonic stamp, idle_seconds-or-None) of the most
        # recent heartbeat. pruned when a user's last surface disconnects.
        self._presence: Dict[str, Tuple[float, Optional[float]]] = {}

    # --- subscription (websocket connections, in-flight sends) ---------------

    def subscribe(self, user_uuid: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_CAP)
        self._subscribers.setdefault(user_uuid, set()).add(queue)
        return queue

    def unsubscribe(self, user_uuid: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(user_uuid)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del self._subscribers[user_uuid]
                self._presence.pop(user_uuid, None)

    # --- presence (docs/ROOMS_DESIGN.md section 9) ----------------------------

    def note_presence(self, user_uuid: str,
                      idle_seconds: Optional[float] = None) -> None:
        """record a heartbeat from a connected surface. `idle_seconds` is the
        sidecar's OS-level idle clock as the app last saw it; None means the
        app is open but the sidecar was unreachable (dev rigs) - openness is
        then the best signal available and counts as active."""
        idle = None
        if idle_seconds is not None:
            try:
                idle = max(0.0, float(idle_seconds))
            except (TypeError, ValueError):
                idle = None  # a malformed report degrades to 'open = active'
        self._presence[user_uuid] = (time.monotonic(), idle)

    def presence_state(self, user_uuid: str) -> str:
        """'active' | 'idle' | 'absent' - the routing trichotomy.

        absent: no connected surface at all (the app is closed).
        idle:   a surface is connected but the person isn't demonstrably
                there - the last heartbeat reported idleness past the
                threshold, or heartbeats stopped arriving on a live socket.
        active: a connected surface with a fresh heartbeat that doesn't
                report idleness. a connected surface that has NEVER
                heartbeat is also active - deliberately fail-open, so a
                pre-7a client (which never reports) keeps its lines.
        """
        if not self._subscribers.get(user_uuid):
            return "absent"
        report = self._presence.get(user_uuid)
        if report is None:
            return "active"
        stamp, idle = report
        if time.monotonic() - stamp > _PRESENCE_STALE_SECONDS:
            return "idle"
        if idle is not None and idle >= Config.PRESENCE_IDLE_SECONDS:
            return "idle"
        return "active"

    @staticmethod
    def drain(queue: asyncio.Queue) -> list[dict]:
        """everything currently in a queue, without waiting."""
        items = []
        while True:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    # --- BaseInterface --------------------------------------------------------

    async def start(self):
        """nothing to run - the web server owns the sockets."""

    async def stop(self):
        """drop every subscriber so pending waits end promptly."""
        self._subscribers.clear()

    async def send_message(self, platform_user_id: str, content: str,
                           **kwargs) -> bool:
        """deliver to every subscribed surface for this user. the app's
        platform_user_id IS the user_uuid (the device token already proved
        who they are - there is no foreign account id to map)."""
        payload = {
            "type": "message",
            # a stable id per delivered line: a client that hears the same
            # line on two surfaces (its POST response and its websocket)
            # dedupes by id instead of guessing by content
            "id": str(uuid.uuid4()),
            "author": kwargs.get("speaker") or CHAIR_ID,
            "content": content,
            "at": utc_now().isoformat(),
        }
        # which room this line belongs to (phase 6b): a client rendering one
        # room drops another room's lines instead of interleaving them.
        # absent for senders that don't know (pre-6b payload shape).
        if kwargs.get("stream_id"):
            payload["room"] = kwargs["stream_id"]
        # the tether mirror (phase 7a): a line that originated on another
        # platform - the user's own telegram words, or a council line the
        # router just confirmed there - rides with who spoke it (author_type
        # 'user' renders on the person's side of the room) and where it came
        # from (provenance, matching the history rows' `platform` field).
        # absent on ordinary app-first deliveries, keeping their bytes pre-7a.
        if kwargs.get("author_type"):
            payload["author_type"] = kwargs["author_type"]
        if kwargs.get("source_platform"):
            payload["platform"] = kwargs["source_platform"]
        queues = list(self._subscribers.get(platform_user_id, ()))
        delivered = 0
        for queue in queues:
            try:
                queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning("app subscriber queue full for user %s - "
                               "skipping dead consumer", platform_user_id)
        return delivered > 0

    async def handle_incoming_message(self, message: Any):
        """inbound rides the http handlers, never this hook."""
        raise NotImplementedError(
            "app inbound goes through the /api/v1 handlers")
