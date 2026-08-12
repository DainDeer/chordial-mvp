"""the outbox pump: at-least-once delivery up to the chordial server.

reads the oldest pending events from the store, POSTs them to
/api/v1/sync/events under the device bearer token, and trims everything at
or below the durable ACK cursor the server returns. the server side
(src/services/device_sync.py) guarantees exactly-once apply, so the pump's
only jobs are honesty and persistence: never invent an ACK, never stop
retrying on transient failure, and never crash the sidecar over a network
that will come back.

a 401 means the device was revoked or the token is stale - the pump parks
(kv 'sync_error') until a new handshake replaces the credentials.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from src.sidecar.store import SidecarStore

logger = logging.getLogger(__name__)

_BATCH = 100
_IDLE_SECONDS = 15.0        # how often we look for new pending work
_ERROR_BACKOFF = 60.0       # after a failed push, breathe before retrying


class OutboxPump:
    def __init__(self, store: SidecarStore,
                 session: Optional[aiohttp.ClientSession] = None):
        self.store = store
        self._session = session
        self._owns_session = session is None

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _align_cursor(self, client: aiohttp.ClientSession,
                            server: str, token: str) -> bool:
        """before this store's first push under a device: lift our seq
        numbering above the server's durable cursor. a fresh sidecar db
        under an existing device restarts at seq 1, and everything at or
        below the cursor would be swallowed as a duplicate - acked,
        trimmed, and silently lost. once per (store, device); the kv stamp
        makes it idempotent."""
        device = self.store.get("device_uuid") or token.partition(".")[0]
        if self.store.get("cursor_aligned") == device:
            return True
        try:
            resp = await client.get(
                f"{server}/api/v1/sync/cursor",
                headers={"Authorization": f"Bearer {token}"})
            body = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("cursor fetch failed (will retry): %s", e)
            return False
        if resp.status == 401:
            # revoked device: same park as a rejected push
            self.store.put("sync_error", "device token rejected (401)")
            logger.warning("outbox parked: device token rejected")
            return False
        if resp.status != 200 or not isinstance(body.get("acked_seq"), int):
            logger.warning("cursor fetch got %s: %s", resp.status, body)
            return False
        moved = self.store.align_seq(body["acked_seq"])
        if moved:
            logger.info("outbox renumbered above server cursor %d "
                        "(%d pending)", body["acked_seq"], moved)
        self.store.put("cursor_aligned", device)
        return True

    async def push_once(self) -> Optional[dict]:
        """one push attempt. returns the server's sync result dict, or None
        when there was nothing to send / no credentials / a failure (which
        is logged and left for the next attempt)."""
        events = self.store.pending(_BATCH)
        if not events:
            return None
        token = self.store.get("device_token")
        server = self.store.get("server_url")
        if not token or not server:
            return None            # not handshaken yet; events keep queuing

        client = await self._client()
        if not await self._align_cursor(client, server, token):
            return None
        events = self.store.pending(_BATCH)   # alignment may have renumbered
        try:
            resp = await client.post(
                f"{server}/api/v1/sync/events",
                json={"events": events},
                headers={"Authorization": f"Bearer {token}"})
            body = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("outbox push failed (will retry): %s", e)
            return None

        if resp.status == 401:
            # revoked device: parking is honest, hammering is not
            self.store.put("sync_error", "device token rejected (401)")
            logger.warning("outbox parked: device token rejected")
            return None
        if resp.status != 200:
            logger.warning("outbox push got %s: %s", resp.status, body)
            return None

        self.store.put("sync_error", "")
        acked = body.get("acked_seq")
        if isinstance(acked, int) and acked > 0:
            trimmed = self.store.trim(acked)
            logger.info("outbox: %d event(s) acked through seq %d",
                        trimmed, acked)
        for rejection in body.get("rejected", []):
            # tombstoned server-side; the cursor moved past it, we log it
            logger.warning("sync rejected event %s: %s",
                           rejection.get("id"), rejection.get("error"))
        return body

    async def run(self) -> None:
        """the forever loop: push when there's work, breathe when there
        isn't, back off after failures. cancelled at shutdown."""
        while True:
            try:
                if self.store.get("sync_error"):
                    # parked until a handshake clears it
                    await asyncio.sleep(_ERROR_BACKOFF)
                    continue
                had_work = bool(self.store.pending(1))
                result = await self.push_once()
                if had_work and result is None:
                    await asyncio.sleep(_ERROR_BACKOFF)
                else:
                    await asyncio.sleep(_IDLE_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox pump hiccup; continuing")
                await asyncio.sleep(_ERROR_BACKOFF)
