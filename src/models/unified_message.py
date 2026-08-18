from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from datetime import datetime

from src.utils.timezone_utils import utc_now

@dataclass
class UnifiedMessage:
    """platform-agnostic message format"""
    content: str
    platform_user_id: str
    platform: str
    platform_message_id: str
    attachments: Optional[List[Dict]] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: Optional[datetime] = None  # naive UTC, matches db storage

    # --- routing shape (defaults to the dm shape) -----------------------------
    # where the message arrived: 'dm' (a private channel with the chair -
    # discord, introductions) or 'group' (the user's council room: the app,
    # and the telegram tether since 7a). every group is a private room with
    # exactly one human - the multi-human crew group retired with the
    # per-helper bot ensemble.
    chat_scope: str = "dm"
    # the delivery target for group-scope replies (the app: the user uuid;
    # the tether: the telegram chat id). set only when chat_scope == 'group'.
    group_chat_id: Optional[str] = None
    # phase 6b: bind this turn to a specific room's stream (a cycle room)
    # instead of today's daily room. the caller must have verified the room
    # is the sender's and open; chat_service re-checks at the boundary.
    room_uuid: Optional[str] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = utc_now()
        if self.metadata is None:
            self.metadata = {}
        if self.attachments is None:
            self.attachments = []