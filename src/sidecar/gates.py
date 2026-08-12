"""the gate stack: arithmetic guarantees in front of unprompted speech
(the velvet antler conclusion - budgets are invariants, never model calls).

user-initiated lines (start/pause/switch/finish are RESPONSES) never pass
through here. gates cover only the deer speaking up on her own - the block
ding, the drift check-in, the welcome back - and they answer one question:
is an unprompted word welcome right now?

three gates, all deterministic:
- BLOCKED: a meeting-shaped app is frontmost (the blocklist) - full hush
- QUIET HOURS: a machine-local window (env, e.g. "22-8") where the deer
  never speaks up first
- BUDGET: at most N unprompted lines per rolling hour - presence, not
  commentary, even on a chaotic day

events are never gated: facts always land in the outbox. gates silence
the WORDS, not the record.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

LINES_PER_HOUR = int(os.getenv("SIDECAR_LINES_PER_HOUR", "6"))

DEFAULT_BLOCKLIST = (
    "us.zoom.xos,com.microsoft.teams,com.microsoft.teams2,"
    "com.apple.FaceTime,com.cisco.webexmeetingsapp"
)


def blocklist() -> frozenset[str]:
    raw = os.getenv("SIDECAR_BLOCKLIST", DEFAULT_BLOCKLIST)
    return frozenset(b.strip() for b in raw.split(",") if b.strip())


def parse_quiet_hours(spec: str) -> Optional[tuple[int, int]]:
    """'22-8' -> (22, 8); empty/malformed -> None (no quiet hours). the
    window may wrap midnight; start == end would silence the whole day and
    is treated as malformed."""
    if not spec or "-" not in spec:
        return None
    try:
        start_raw, end_raw = spec.split("-", 1)
        start, end = int(start_raw), int(end_raw)
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return None
    return (start, end)


def in_quiet_hours(hours: Optional[tuple[int, int]], local_hour: int) -> bool:
    if hours is None:
        return False
    start, end = hours
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end   # wraps midnight


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SpeechGates:
    """constructed once; `clock` injectable for tests. the budget window is
    in-memory - a sidecar restart forgiving the count errs on the side of
    a deer who can still say welcome back."""

    def __init__(self, clock: Callable[[], datetime] = _utc_now,
                 lines_per_hour: int = LINES_PER_HOUR):
        self.clock = clock
        self.lines_per_hour = lines_per_hour
        self.quiet_hours = parse_quiet_hours(
            os.getenv("SIDECAR_QUIET_HOURS", ""))
        self._spent: list[datetime] = []

    def allow(self, *, blocked: bool) -> bool:
        """may the deer speak up unprompted right now?"""
        if blocked:
            return False
        now = self.clock()
        if in_quiet_hours(self.quiet_hours, now.astimezone().hour):
            return False
        cutoff = now - timedelta(hours=1)
        self._spent = [t for t in self._spent if t > cutoff]
        return len(self._spent) < self.lines_per_hour

    def spend(self) -> None:
        """record one unprompted line against the rolling budget."""
        self._spent.append(self.clock())
