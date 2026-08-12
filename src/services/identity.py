"""the user/stream identity split (docs/ROOMS_DESIGN.md section 11).

two different questions, two different keys:

- `user_id` answers "WHOSE reality?" - workspace, memories, profile, budgets,
  delivery links, usage attribution.
- `stream_id` answers "WHICH conversation?" - the engine's opaque event-stream
  key: today the legacy one-stream-per-user (where it happens to equal the
  user uuid), in phase 2b a room id.

the engine stays user-agnostic on purpose (identity is app vocabulary), so
the user id rides chordial's app-controlled channels: `Stimulus.extras` /
`Briefing.extras` / `ToolContext.metadata`, always under USER_ID_KEY. every
place that used to treat a stream id as a user uuid goes through these
accessors instead - the fallback to stream_id keeps legacy streams (and any
not-yet-threaded caller) working, and is exactly the equality that rooms
break.

usage events are the one seam the app channels don't reach (the dainframe's
ProviderCallUsage/AgentRunTrace carry only stream_id - upstream finding
noted in the design doc); `resolve_stream_user` is the recorder's hook, and
phase 2b implements it via the rooms table.
"""
from __future__ import annotations

USER_ID_KEY = "user_id"


def user_of_stimulus(stimulus) -> str:
    """the user a stimulus concerns."""
    return (stimulus.extras or {}).get(USER_ID_KEY) or stimulus.stream_id


def user_of_briefing(briefing) -> str:
    """the user a briefing concerns (extras threaded by ChordialContext)."""
    return (briefing.extras or {}).get(USER_ID_KEY) or briefing.stream_id


def user_of_context(context) -> str:
    """the user a tool call acts for (metadata threaded by HelperAgent and
    the reconciler)."""
    return (context.metadata or {}).get(USER_ID_KEY) or context.stream_id


def resolve_stream_user(stream_id: str) -> str:
    """map an engine stream id to its user - the usage recorder's (and the
    event store's) resolver. a room stream resolves through the rooms table;
    anything unknown falls back to the legacy equality (pre-rooms streams
    ARE user uuids - and the grandfathered legacy room's uuid is the user
    uuid anyway, so both paths agree)."""
    from src.services.rooms import get_room_store
    user = get_room_store().user_of_room(stream_id)
    return user or stream_id
