from typing import Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass, field
from src.core.temporal_context import TemporalContext

@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    temporal_context: Optional[Dict[str, str]] = None

    def __post_init__(self):
        # generate temporal context if not provided
        if self.temporal_context is None:
            context = TemporalContext()
            self.temporal_context = context.get_detailed_context()

@dataclass
class Conversation:
    user_id: str
    platform: str
    messages: List[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    
    def add_message(self, role: str, content: str):
        """add a message to the conversation"""
        self.messages.append(Message(role=role, content=content))
    
    def get_history(self, max_messages: int = 10, include_temporal: bool = True) -> List[Dict[str, str]]:
        """get conversation history in format suitable for ai providers"""
        # return the last n messages
        recent_messages = self.messages[-max_messages:]
        history = []
        
        for i, msg in enumerate(recent_messages):
            if include_temporal and i == 0:
                # for the first message in history, add a system note about when the conversation started
                if msg.temporal_context:
                    context_note = {
                        "role": "system",
                        "content": f"conversation context: this exchange started during {msg.temporal_context['time_of_day']} on {msg.temporal_context['day_of_week']}"
                    }
                    history.append(context_note)
            
            # add the actual message
            history.append({"role": msg.role, "content": msg.content})
            
            # add temporal context between messages if significant time passed
            if include_temporal and i < len(recent_messages) - 1:
                next_msg = recent_messages[i + 1]
                time_diff = next_msg.timestamp - msg.timestamp
                
                # if more than 30 minutes passed, note it
                if time_diff.total_seconds() > 1800:
                    hours_passed = time_diff.total_seconds() / 3600
                    if hours_passed >= 24:
                        time_note = f"[{int(hours_passed / 24)} days later]"
                    elif hours_passed >= 1:
                        time_note = f"[{int(hours_passed)} hours later]"
                    else:
                        minutes = int(time_diff.total_seconds() / 60)
                        time_note = f"[{minutes} minutes later]"
                    
                    history.append({
                        "role": "system",
                        "content": f"{time_note} now it's {next_msg.temporal_context['time_of_day']} on {next_msg.temporal_context['day_of_week']}"
                    })
        
        return history

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