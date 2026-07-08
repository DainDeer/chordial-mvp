from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any

from src.utils.timezone_utils import utc_now

@dataclass
class Message:
    role: str  # "user" or "assistant" or "system"
    content: str
    timestamp: datetime = field(default_factory=utc_now)  # naive UTC, matches db storage
    message_type: str = "conversation"  # "conversation", "scheduled", or "system"
    db_id: Optional[int] = None  # for tracking in database
    
    def to_dict(self) -> Dict[str, Any]:
        """convert to dict for api calls"""
        return {
            "role": self.role,
            "content": self.content
        }
    
    @classmethod
    def from_event(cls, event) -> 'Message':
        """bridge an event-log Event into the prompt-side Message shape.

        a temporary compatibility seam: PromptService still speaks Message;
        once it consumes Events directly (action-aware rendering), this module
        retires."""
        return cls(
            role=event.role,
            content=event.content,
            timestamp=event.created_at,
            message_type=event.message_type or "conversation",
            db_id=event.db_id
        )