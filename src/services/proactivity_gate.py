"""the proactivity gate: the non-interaction guard.

pure event-log arithmetic, consulted by the scheduler BEFORE the director or
any generation - a denied tick costs one db read and zero tokens. never
generate a proactive message just to throw it away.

"unanswered proactive messages" = agent `kind='message'` events with
`message_type='scheduled'` that occur after the user's last message in the
window. `action`/`note` rows never count on either side of that split
(existing invariant: a note is never "the assistant replied", and a tool
action never masquerades as outreach).

three stacked rules, checked in order:
  1. crew cap - GATE_CREW_CAP unanswered proactive messages, from anyone,
     silences the whole crew. being ignored is a signal to everyone, not a
     budget each helper spends separately.
  2. per-helper cap - GATE_PER_HELPER_CAP unanswered proactive messages from
     THIS helper silences just them (other helpers may still be clear).
  3. exponential backoff - each unanswered message doubles the required
     quiet period since the newest one: GATE_BASE_INTERVAL_HOURS * 2**(n-1),
     so 3h -> 6h -> 12h before the crew cap ends the chain at 4.

any user message, anywhere, resets all three (it moves the anchor forward,
emptying `unanswered`).
"""
from dataclasses import dataclass
from typing import List

from src.managers.event_log import Event, EventLog
from src.utils.timezone_utils import utc_now
from config import Config


@dataclass
class OutreachDecision:
    allowed: bool
    reason: str  # logged on denial; later surfaced to the ai director


def _unanswered_proactive(events: List[Event]) -> List[Event]:
    """agent scheduled messages after the newest user message in the window,
    oldest -> newest. no user message in the window at all means the whole
    window counts (there is nothing to have reset the chain)."""
    last_user_idx = -1
    for i, event in enumerate(events):
        if event.kind == "message" and event.author_type == "user":
            last_user_idx = i
    return [
        event for event in events[last_user_idx + 1:]
        if event.kind == "message" and event.message_type == "scheduled"
    ]


class ProactivityGate:
    """stateless - construct freely, one call per scheduler tick."""

    def check(self, log: EventLog, helper_id: str) -> OutreachDecision:
        events = log.recent(Config.MAX_HISTORY_MESSAGES)
        unanswered = _unanswered_proactive(events)

        if len(unanswered) >= Config.GATE_CREW_CAP:
            return OutreachDecision(False, "crew cap: quiet until they speak")

        by_helper = sum(1 for e in unanswered if e.author == helper_id)
        if by_helper >= Config.GATE_PER_HELPER_CAP:
            return OutreachDecision(False, f"{helper_id} cap: their turn is over")

        if unanswered:
            required_hours = Config.GATE_BASE_INTERVAL_HOURS * (2 ** (len(unanswered) - 1))
            newest = unanswered[-1].created_at
            elapsed_hours = (utc_now() - newest).total_seconds() / 3600
            if elapsed_hours < required_hours:
                return OutreachDecision(
                    False,
                    f"backoff: {required_hours}h required, {elapsed_hours:.1f}h elapsed",
                )

        return OutreachDecision(True, "clear")
