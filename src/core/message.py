from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any

@dataclass
class Message:
    role: str  # "user" or "assistant" or "system"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    message_type: str = "conversation"  # "conversation", "scheduled", or "system"
    db_id: Optional[int] = None  # for tracking in database
    
    def to_dict(self) -> Dict[str, Any]:
        """convert to dict for api calls"""
        return {
            "role": self.role,
            "content": self.content
        }
    
    @classmethod
    def from_db(cls, db_msg) -> 'Message':
        """create message from database model"""
        return cls(
            role=db_msg.role,
            content=db_msg.content,
            timestamp=db_msg.created_at,
            message_type=db_msg.message_type or "conversation",
            db_id=db_msg.id
        )