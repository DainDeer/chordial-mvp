"""the shared vocabulary of orchestration: what comes IN (a Stimulus), the
script the director produces (a sequence of ScriptLines), and what goes back
OUT (a Deliverable).

kept in their own module (rather than inside orchestrator.py) so the platform
adapter that CONSTRUCTS stimuli and the orchestrator that CONSUMES them can
share one definition without importing each other's logic. see
docs/V3_DESIGN.md section 6 (the director) and section 1 (group vs dm routing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Stimulus:
    """something that might make one or more helpers act."""
    kind: str                        # 'user_message' | 'scheduled_tick' | 'introduction' | 'curation_due'
    user_uuid: str
    platform: Optional[str] = None
    content: Optional[str] = None    # user_message only
    user_name: Optional[str] = None
    user_timezone: Optional[str] = None

    # --- v3 group/dm routing --------------------------------------------------
    # where this happened. 'dm' = a private 1:1 with one helper (the receiving
    # bot IS the speaker, so the reply returns synchronously). 'group' = the
    # shared channel (replies are delivered out-of-band, each via its own bot).
    chat_scope: str = "dm"
    # delivery target for chat_scope='group' (the telegram group's chat id).
    group_chat_id: Optional[str] = None
    # for chat_scope='dm', which helper the 1:1 is with (also the lone speaker).
    dm_helper: Optional[str] = None
    # helper ids the user explicitly @-addressed in a group message, in order.
    mentioned: List[str] = field(default_factory=list)

    # --- introduction ---------------------------------------------------------
    # for kind='introduction', which helper is meeting the user this activation.
    intro_helper: Optional[str] = None


@dataclass
class ScriptLine:
    """one turn in the director's script: who speaks, and the stage direction
    for them. `style='brief'` asks for a short reaction rather than a full reply."""
    speaker: str                     # helper id
    cue: Optional[str] = None        # one-line "why you're speaking / what angle"
    style: str = "full"              # 'full' | 'brief'


@dataclass
class Script:
    """the ordered sequence of speakers for one activation. delivered in order;
    each speaker is briefed AFTER the previous line is recorded, so a later
    speaker genuinely reacts to an earlier one."""
    lines: List[ScriptLine] = field(default_factory=list)


@dataclass
class Deliverable:
    """what the caller (platform adapter / scheduler) gets back.

    a dm/single-speaker activation returns `text` for the receiving interface
    to send. a group activation delivers each line out-of-band through the
    speaker-aware router and comes back `handled=True` with text=None - the
    interface sends nothing, because the bots already spoke for themselves."""
    text: Optional[str] = None
    refused: bool = False
    errored: bool = False
    handled: bool = False
