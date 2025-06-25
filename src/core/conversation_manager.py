from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass, field
from src.core.temporal_context import TemporalContext
from src.database.database import get_db
from src.database.models import ConversationHistory, CompressedMessage
from src.services.compressor_service import CompressorService
import logging

logger = logging.getLogger(__name__)

@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    message_type: str = "conversation"  # "conversation" or "scheduled"


@dataclass
class Conversation:
    user_uuid: str  # internal chordial user ID
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
                ConversationHistory.user_uuid == self.user_uuid,
                ConversationHistory.platform == self.platform
            ).order_by(ConversationHistory.created_at.desc()).limit(50).all()
            
            # reverse to get chronological order
            history.reverse()
            
            # convert to Message objects
            for msg in history:
                self.messages.append(Message(
                    role=msg.role,
                    content=msg.content,
                    timestamp=msg.created_at,
                    message_type=msg.message_type
                ))
            
            if self.messages:
                logger.info(f"loaded {len(self.messages)} messages from database for user {self.user_uuid}")
    
    def add_message(self, role: str, content: str, message_type: str = "conversation"):
        """add a message to the conversation and save to database"""
        msg = Message(role=role, content=content, message_type=message_type)
        self.messages.append(msg)
        
        # save to database
        with get_db() as db:
            db_msg = ConversationHistory(
                user_uuid=self.user_uuid,
                platform=self.platform,
                role=role,
                content=content,
                message_type=message_type
            )
            db.add(db_msg)
            db.flush()  # flush to get the id

            # store the message id for later compression
            msg.db_id = db_msg.id

            db.commit()
            logger.debug(f"saved {role} message ({message_type}) to database for user {self.user_uuid}")


    # Add a separate method to compress existing messages
    async def compress_last_message(self):
        """compress the last message that was added"""
        from src.services.compressor_service import CompressorService
        
        if not self.messages:
            return
        
        last_msg = self.messages[-1]
        if not hasattr(last_msg, 'db_id'):
            logger.warning("last message has no db_id, skipping compression")
            return
        
        compressor = CompressorService()
        compressed_content = await compressor.compress_message(last_msg.content, last_msg.role)
        
        # store compressed version
        await compressor.store_compressed_message(
            conversation_history_id=last_msg.db_id,
            user_uuid=self.user_uuid,
            platform=self.platform,
            role=last_msg.role,
            original_content=last_msg.content,
            compressed_content=compressed_content
        )

    async def get_compressed_conversation_history(
        self, 
        limit: int = 10,
        include_temporal: bool = True
    ) -> List[Dict[str, str]]:
        """get conversation history using compressed messages"""
        
        compressor = CompressorService()
        compressed_history = await compressor.get_compressed_history(
            self.user_uuid,
            self.platform,
            limit=limit
        )
        
        # add temporal context if needed
        if include_temporal and compressed_history:
            context_note = {
                "role": "system",
                "content": f"""Current context: {TemporalContext.get_context_string(datetime.now())}
use this temporal awareness naturally in your response when relevant, but don't always mention the time."""
            }
            compressed_history.append(context_note)
        
        return compressed_history
    
    async def get_hybrid_conversation_history(
    self, 
    limit: int = 20,
    full_message_count: int = 5,
    include_temporal: bool = True
) -> List[Dict[str, str]]:
        """
        get conversation history using a mix of full and compressed messages.
        
        args:
            limit: total number of messages to return
            full_message_count: number of most recent messages to keep uncompressed
            include_temporal: whether to add temporal context
        
        returns:
            list of messages with most recent N as full, older as compressed
        """
        
        with get_db() as db:
            # first, get the most recent messages (we'll keep some full)
            recent_messages = db.query(ConversationHistory).filter(
                ConversationHistory.user_uuid == self.user_uuid,
                ConversationHistory.platform == self.platform
            ).order_by(ConversationHistory.created_at.desc()).limit(limit).all()
            
            # reverse to get chronological order
            recent_messages.reverse()
            
            if not recent_messages:
                return []
            
            history = []
            
            # split into full and compressed portions
            if len(recent_messages) <= full_message_count:
                # all messages will be full
                for msg in recent_messages:
                    history.append({
                        "role": msg.role,
                        "content": msg.content,
                        "message_type": "raw",
                        "timestamp": msg.created_at
                    })
            else:
                # older messages (to be compressed)
                older_messages = recent_messages[:-full_message_count]
                # most recent messages (keep full)
                newest_messages = recent_messages[-full_message_count:]
                
                # get compressed versions of older messages
                older_msg_ids = [msg.id for msg in older_messages]
                
                compressed_messages = db.query(CompressedMessage).filter(
                    CompressedMessage.conversation_history_id.in_(older_msg_ids)
                ).all()
                
                # create a mapping for easy lookup
                compressed_map = {cm.conversation_history_id: cm for cm in compressed_messages}
                
                # add older messages (compressed if available, otherwise full)
                for msg in older_messages:
                    if msg.id in compressed_map:
                        # use compressed version
                        history.append({
                            "role": msg.role,
                            "content": compressed_map[msg.id].compressed_content,
                            "message_type": "summary",
                            "timestamp": msg.created_at
                        })
                    else:
                        # fallback to full if no compressed version exists yet
                        logger.warning(f"no compressed version for message {msg.id}, using full")
                        history.append({
                            "role": msg.role,
                            "content": msg.content,
                            "message_type": "raw",
                            "timestamp": msg.created_at
                        })
                
                # add newest messages (always full)
                for msg in newest_messages:
                    history.append({
                        "role": msg.role,
                        "content": msg.content,
                        "message_type": "raw",
                        "timestamp": msg.created_at
                    })
            
            # add temporal context if needed
            if include_temporal and history:
                context_note = {
                    "role": "system",
                    "content": f"""Current context: {TemporalContext.get_context_string(datetime.now())}
    use this temporal awareness naturally in your response when relevant, but don't always mention the time."""
                }
                history.append(context_note)
            
            return history

    def get_history(self, max_messages: int = 10, include_temporal: bool = True) -> List[Dict[str, str]]:
        """
        simple method to get uncompressed conversation history.
        mainly for debugging or when compression isn't needed.
        """
        recent_messages = self.messages[-max_messages:]
        history = []
        
        for msg in recent_messages:
            history.append({"role": msg.role, "content": msg.content})
        
        # add temporal context if needed
        if include_temporal and history:
            context_note = {
                "role": "system",
                "content": f"""Current context: {TemporalContext.get_context_string(datetime.now())}
    use this temporal awareness naturally in your response when relevant, but don't always mention the time."""
            }
            history.append(context_note)
        
        return history

class ConversationManager:
    """manages conversations across all users and platforms"""
    
    def __init__(self):
        # in-memory cache of active conversations
        self._conversations: Dict[str, Conversation] = {}
        self.max_messages_in_memory = 100  # keep last 100 messages in memory
        self.max_messages_in_database = 1000  # keep last 1000 in database
    
    def _get_key(self, user_uuid: str, platform: str) -> str:
        """generate a unique key for storing conversations"""
        return f"{platform}:{user_uuid}"
    
    async def get_or_create(self, user_uuid: str, platform: str) -> Conversation:
        """get existing conversation or create a new one"""
        key = self._get_key(user_uuid, platform)
        
        if key not in self._conversations:
            self._conversations[key] = Conversation(
                user_uuid=user_uuid,
                platform=platform
            )
        
        # trim messages if too many in memory
        conv = self._conversations[key]
        if len(conv.messages) > self.max_messages_in_memory:
            # keep only the most recent messages in memory
            conv.messages = conv.messages[-self.max_messages_in_memory:]
        
        return conv
    
    async def clear_conversation(self, user_uuid: str, platform: str):
        """clear a user's conversation history"""
        key = self._get_key(user_uuid, platform)
        if key in self._conversations:
            del self._conversations[key]
        
        # also clear from database
        with get_db() as db:
            db.query(ConversationHistory).filter(
                ConversationHistory.user_uuid == user_uuid,
                ConversationHistory.platform == platform
            ).delete()
            db.commit()
            logger.info(f"cleared conversation history for user {user_uuid} on {platform}")
    
    async def cleanup_old_messages(self):
        """cleanup old messages from database (run this periodically)"""
        with get_db() as db:
            # for each user, keep only the most recent N messages
            users = db.query(ConversationHistory.user_uuid).distinct().all()
            
            for (user_uuid,) in users:
                # get all messages for this user, ordered by date
                messages = db.query(ConversationHistory).filter(
                    ConversationHistory.user_uuid == user_uuid
                ).order_by(ConversationHistory.created_at.desc()).all()
                
                # delete messages beyond the limit
                if len(messages) > self.max_messages_in_database:
                    for msg in messages[self.max_messages_in_database:]:
                        db.delete(msg)
            
            db.commit()
            logger.info("cleaned up old messages from database")