from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass, field
from src.core.temporal_context import TemporalContext
from src.database.database import get_db
from src.database.models import ConversationHistory
import logging

logger = logging.getLogger(__name__)

@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    context: Optional[Dict[str, Any]] = None
    message_type: str = "conversation"  # "conversation" or "scheduled"

    def __post_init__(self):
        # generate context if not provided
        if self.context is None:
            temporal = TemporalContext()
            self.context = temporal.get_detailed_context()
        
        # ensure message_type is in context
        if 'message_type' not in self.context:
            self.context['message_type'] = self.message_type

@dataclass
class Conversation:
    user_id: str
    platform: str
    messages: List[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        # load conversation history from database on creation
        self._load_from_database()
    
    def _load_from_database(self):
        """load conversation history from database"""
        with get_db() as db:
            # get the last 50 messages for this user
            history = db.query(ConversationHistory).filter(
                ConversationHistory.user_id == self.user_id,
                ConversationHistory.platform == self.platform
            ).order_by(ConversationHistory.created_at.desc()).limit(50).all()
            
            # reverse to get chronological order
            history.reverse()
            
            # convert to Message objects
            for msg in history:
                message_type = msg.context.get('message_type', 'conversation') if msg.context else 'conversation'
                self.messages.append(Message(
                    role=msg.role,
                    content=msg.content,
                    timestamp=msg.created_at,
                    context=msg.context,
                    message_type=message_type
                ))
            
            if self.messages:
                logger.info(f"loaded {len(self.messages)} messages from database for user {self.user_id}")
    
    def add_message(self, role: str, content: str, message_type: str = "conversation"):
        """add a message to the conversation and save to database"""
        msg = Message(role=role, content=content, message_type=message_type)
        self.messages.append(msg)
        
        # save to database
        with get_db() as db:
            db_msg = ConversationHistory(
                user_id=self.user_id,
                platform=self.platform,
                role=role,
                content=content,
                context=msg.context
            )
            db.add(db_msg)
            db.commit()
            logger.debug(f"saved {role} message ({message_type}) to database for user {self.user_id}")
    
    def get_history(self, max_messages: int = 10, include_temporal: bool = True) -> List[Dict[str, str]]:
        """get conversation history in format suitable for ai providers"""
        # return the last n messages
        recent_messages = self.messages[-max_messages:]
        history = []
        
        for i, msg in enumerate(recent_messages):
            if include_temporal and i == 0:
                # for the first message in history, add a system note about when the conversation started
                if msg.context:
                    context_note = {
                        "role": "system",
                        "content": f"conversation context: this exchange started during {msg.context.get('time_of_day', 'unknown')} on {msg.context.get('day_of_week', 'unknown')}"
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
                    
                    next_context = next_msg.context or {}
                    history.append({
                        "role": "system",
                        "content": f"{time_note} now it's {next_context.get('time_of_day', 'unknown')} on {next_context.get('day_of_week', 'unknown')}"
                    })
        
        return history

class ConversationManager:
    """manages conversations across all users and platforms"""
    
    def __init__(self):
        # in-memory cache of active conversations
        self._conversations: Dict[str, Conversation] = {}
        self.max_messages_in_memory = 100  # keep last 100 messages in memory
        self.max_messages_in_database = 1000  # keep last 1000 in database
    
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
        
        # trim messages if too many in memory
        conv = self._conversations[key]
        if len(conv.messages) > self.max_messages_in_memory:
            # keep only the most recent messages in memory
            conv.messages = conv.messages[-self.max_messages_in_memory:]
        
        return conv
    
    async def clear_conversation(self, user_id: str, platform: str):
        """clear a user's conversation history"""
        key = self._get_key(user_id, platform)
        if key in self._conversations:
            del self._conversations[key]
        
        # also clear from database
        with get_db() as db:
            db.query(ConversationHistory).filter(
                ConversationHistory.user_id == user_id,
                ConversationHistory.platform == platform
            ).delete()
            db.commit()
            logger.info(f"cleared conversation history for user {user_id} on {platform}")
    
    async def cleanup_old_messages(self):
        """cleanup old messages from database (run this periodically)"""
        with get_db() as db:
            # for each user, keep only the most recent N messages
            users = db.query(ConversationHistory.user_id).distinct().all()
            
            for (user_id,) in users:
                # get all messages for this user, ordered by date
                messages = db.query(ConversationHistory).filter(
                    ConversationHistory.user_id == user_id
                ).order_by(ConversationHistory.created_at.desc()).all()
                
                # delete messages beyond the limit
                if len(messages) > self.max_messages_in_database:
                    for msg in messages[self.max_messages_in_database:]:
                        db.delete(msg)
            
            db.commit()
            logger.info("cleaned up old messages from database")