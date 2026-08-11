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
from typing import Any, Dict, Set

from src.providers.platforms.base import BaseInterface
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# a slow websocket consumer must never block delivery confirmation; a queue
# that somehow backs up this far is a dead consumer wearing a live socket
_QUEUE_CAP = 256


class AppInterface(BaseInterface):
    """fan-out delivery to the user's connected app surfaces."""

    platform = "app"

    def __init__(self, chat_service=None):
        super().__init__(chat_service)
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

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
            "author": kwargs.get("speaker") or "chordial",
            "content": content,
            "at": utc_now().isoformat(),
        }
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
