from typing import Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass, field

@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class Conversation:
    user_id: str
    platform: str
    messages: List[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    
    def add_message(self, role: str, content: str):
        """add a message to the conversation"""
        self.messages.append(Message(role=role, content=content))
    
    def get_history(self, max_messages: int = 10) -> List[Dict[str, str]]:
        """get conversation history in format suitable for ai providers"""
        # return the last n messages
        recent_messages = self.messages[-max_messages:]
        return [{"role": msg.role, "content": msg.content} for msg in recent_messages]

class ConversationManager:
    """manages conversations across all users and platforms"""
    
    def __init__(self):
        # in-memory storage for now, will be replaced with database later
        self._conversations: Dict[str, Conversation] = {}
    
    def _get_key(self, user_id: str, platform: str) -> str:
        """generate a unique key for storing conversations"""
        return f"{platform}:{user_id}"
    
    async def get_or_create(self, user_id: str, platform: str) -> Conversation:
        """get existing conversation or create a new one"""
        key = self._get_key(user_id, platform)
        
        if key not in self._conversations:
            self._conversations[key] = Conversation(
                user_id=user_id,
                platform=platform
            )
        
        return self._conversations[key]
    
    async def clear_conversation(self, user_id: str, platform: str):
        """clear a user's conversation history"""
        key = self._get_key(user_id, platform)
        if key in self._conversations:
            del self._conversations[key]