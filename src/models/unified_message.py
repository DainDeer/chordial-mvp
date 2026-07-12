from dataclasses import dataclass, field
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

    # --- v3 multi-bot / group-chat routing (all default to the v2 dm shape) ---
    # where the message arrived: 'dm' (a 1:1 chat with one helper's bot) or
    # 'group' (the shared crew group chat).
    chat_scope: str = "dm"
    # the telegram group's chat id, set only when chat_scope == 'group'.
    group_chat_id: Optional[str] = None
    # the helper id whose bot RECEIVED this message (which door it came through).
    via_bot: Optional[str] = None
    # for a dm, which helper's bot it is (== via_bot for a dm).
    dm_helper: Optional[str] = None
    # helper ids explicitly @-addressed in a group message, in mention order.
    mentioned: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = utc_now()
        if self.metadata is None:
            self.metadata = {}
        if self.attachments is None:
            self.attachments = []