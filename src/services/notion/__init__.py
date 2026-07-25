"""a thin async notion rest client, kept for the future per-user mirror.

notion is no longer a workspace backend (the native in-db workspace is the
only system of record - docs/NATIVE_WORKSPACE_DESIGN.md). this package is
deliberately down to one module: the planned extension is a ONE-WAY mirror
that pushes workspace items (plans, goals, wins, notes, occasions) into a
user's own notion, authenticated by a per-user integration token - which is
why `NotionClient` takes `api_key` as a constructor argument. nothing in the
live service imports this today.
"""
from .client import NotionClient, NotionError, get_client

__all__ = ["NotionClient", "NotionError", "get_client"]
