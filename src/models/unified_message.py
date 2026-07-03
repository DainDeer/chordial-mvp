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

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = utc_now()
        if self.metadata is None:
            self.metadata = {}
        if self.attachments is None:
            self.attachments = []