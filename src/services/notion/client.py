"""async notion rest client.

a deliberately small surface: the four calls the dainframe tools need (query,
create, update, retrieve) plus paged querying. one shared httpx.AsyncClient is
created lazily and reused - handlers run concurrently under asyncio.gather, and
a shared client with a connection pool is the right shape for that.

errors from notion (4xx/5xx) are raised as NotionError with the api's message
so the tool layer can turn them into model-readable strings. the agent loop
already catches exceptions and feeds them back to the model, so tools are free
to let NotionError propagate.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from config import Config

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.notion.com/v1"


class NotionError(Exception):
    """a notion api call failed. `status` is the http status; `code` is
    notion's machine-readable error code (e.g. 'object_not_found',
    'validation_error', 'unauthorized') when present."""

    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


class NotionClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        version: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self._api_key = api_key or Config.NOTION_API_KEY
        if not self._api_key:
            raise NotionError("no notion api key configured (set NOTION_API_KEY)")
        self._version = version or Config.NOTION_API_VERSION
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=_API_ROOT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Notion-Version": self._version,
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, method: str, path: str, json: Optional[dict] = None) -> dict:
        try:
            resp = await self._http().request(method, path, json=json)
        except httpx.HTTPError as e:
            raise NotionError(f"could not reach notion: {e}") from e

        if resp.status_code >= 400:
            code = None
            message = resp.text
            try:
                body = resp.json()
                code = body.get("code")
                message = body.get("message", message)
            except Exception:
                pass
            logger.warning("notion %s %s -> %s (%s)", method, path, resp.status_code, code)
            raise NotionError(message, status=resp.status_code, code=code)

        return resp.json()

    # --- database queries --------------------------------------------------

    async def query_database(
        self,
        database_id: str,
        *,
        filter: Optional[dict] = None,
        sorts: Optional[list[dict]] = None,
        page_size: int = 25,
        start_cursor: Optional[str] = None,
    ) -> dict:
        """one page of results. returns the raw notion response
        ({'results': [...], 'has_more': bool, 'next_cursor': str|None})."""
        payload: dict[str, Any] = {"page_size": max(1, min(page_size, 100))}
        if filter:
            payload["filter"] = filter
        if sorts:
            payload["sorts"] = sorts
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return await self._request("POST", f"/databases/{database_id}/query", json=payload)

    async def query_all(
        self,
        database_id: str,
        *,
        filter: Optional[dict] = None,
        sorts: Optional[list[dict]] = None,
        limit: int = 25,
    ) -> list[dict]:
        """follow pagination until `limit` pages are collected (or notion runs
        out). `limit` is a row cap, not a request cap - keeps prompts bounded."""
        rows: list[dict] = []
        cursor: Optional[str] = None
        while len(rows) < limit:
            batch = await self.query_database(
                database_id,
                filter=filter,
                sorts=sorts,
                page_size=min(100, limit - len(rows)),
                start_cursor=cursor,
            )
            rows.extend(batch.get("results", []))
            if not batch.get("has_more"):
                break
            cursor = batch.get("next_cursor")
        return rows[:limit]

    # --- pages -------------------------------------------------------------

    async def create_page(
        self,
        database_id: str,
        properties: dict,
        *,
        children: Optional[list[dict]] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return await self._request("POST", "/pages", json=payload)

    async def update_page(self, page_id: str, properties: dict) -> dict:
        return await self._request(
            "PATCH", f"/pages/{page_id}", json={"properties": properties}
        )

    async def retrieve_page(self, page_id: str) -> dict:
        return await self._request("GET", f"/pages/{page_id}")


# a lazily-built process-wide singleton so tools share one connection pool.
_singleton: Optional[NotionClient] = None


def get_client() -> NotionClient:
    global _singleton
    if _singleton is None:
        _singleton = NotionClient()
    return _singleton
