"""the deer's authored voice: small, quiet, and written ahead of time.

the sidecar makes no model calls (no keys ever live on the device), so
every word the deer says here was authored - the velvet antler principle:
authored lines first, generated language later as a server-batched pool.
lines are deliberately small; the deer is presence, not commentary, and
during a block she must never become another task.

voice notes (docs/COUNCIL_VOICE_REFERENCE.md, vel-adjacent but quieter):
lowercase, companionable, zero coach pressure. a block completed is 25
minutes of effort, not a promise kept - completions celebrate showing up;
abandons carry NO disappointment, stopping early is information, not
failure.
"""
from __future__ import annotations

import random
from typing import Optional

LINE_POOLS: dict[str, list[str]] = {
    "session_start": [
        "okay. i'm right here. 🦌 *settles into a loaf*",
        "clock's running. you work, i'll loaf.",
        "*loafs companionably* go on then. i've got the watch.",
        "starting now. one block, nothing more heroic than that.",
        "here we go. i'll be pleasantly irrelevant until the ding.",
        "*tucks legs in* i'm set. your move.",
    ],
    "session_complete": [
        "*ding* 🦌 that's the block. you showed up for the whole thing.",
        "done!! *ears perk* stretch something before the next one?",
        "block complete. logged and witnessed. ✨",
        "that's time! *approving ear flick* water, then whatever's next.",
        "*lifts head* and THAT is a finished block. felt long? it counts double.",
        "ding. 🦌 you did the sitting-down-and-doing-it part, which is the whole trick.",
    ],
    "session_abandoned": [
        "stopped early - that's information, not a verdict. 🦌",
        "okay. clock's off. the minutes you did still happened.",
        "*unhurried* we stop when stopping's right. i'm still here.",
        "block closed early. no notes. want to tell me or just breathe?",
        "noted and released. early stops are allowed here.",
    ],
}


def pick_line(moment: str, last: Optional[str] = None) -> str:
    """a random authored line for this moment, never the same one twice in a
    row (the deer repeating herself verbatim breaks the being-alive spell)."""
    pool = LINE_POOLS.get(moment)
    if not pool:
        return ""
    candidates = [line for line in pool if line != last] or pool
    return random.choice(candidates)
