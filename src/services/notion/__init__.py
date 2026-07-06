"""notion integration for the dainframe workspace.

`client` is a thin async wrapper over the notion rest api; `schema` encodes the
dainframe's tasks/projects/cycles databases (enums, property builders, page
formatters). the model-facing tools live in src/services/tools/notion_tools.py
and lean on both.
"""
from .client import NotionClient, NotionError, get_client

__all__ = ["NotionClient", "NotionError", "get_client"]
