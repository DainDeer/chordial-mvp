"""the acting-helper context: which persona is running the current tool loop.

tools are shared Tool objects in one registry; a handler's signature is
(tool_input, user_uuid) and deliberately doesn't carry persona identity - the
model never chooses who it is. but a few tools DO need to know which helper is
acting: save_memory attributes `created_by` (so a sibling's memory can later
render as "(from aria) ..."), and complete_introduction records state for the
helper doing the introducing.

rather than thread an extra arg through every handler and every call site, the
acting helper rides in a contextvar that AgentService sets around the tool
loop. contextvars are the async-safe mechanism here: asyncio.gather copies the
current context into each task at creation, so parallel tool calls in one turn
all see the helper that was set before the gather. handlers that don't care
simply never read it.

default is 'chordial' - the single-helper (v2) world, where every save is
chordial's, needs no wiring to keep behaving exactly as before.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

_acting_helper: contextvars.ContextVar[str] = contextvars.ContextVar(
    "acting_helper", default="chordial"
)


def current_helper() -> str:
    """the helper id running the current tool loop (default 'chordial')."""
    return _acting_helper.get()


@contextmanager
def acting_as(helper_id: str):
    """bind the acting helper for the duration of a tool loop. AgentService.run
    wraps its loop in this; the token reset makes it re-entrant and leak-free."""
    token = _acting_helper.set(helper_id)
    try:
        yield
    finally:
        _acting_helper.reset(token)
