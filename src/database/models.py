from sqlalchemy import Column, String, DateTime, JSON, Boolean, ForeignKey, Integer, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

Base = declarative_base()

class User(Base):
    """main user table - our source of truth for users across platforms"""
    __tablename__ = 'users'
    
    # primary key is a uuid so we're not tied to any platform's id system
    uuid = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # what the user wants to be called
    preferred_name = Column(String, nullable=True)
    
    # when they joined
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # user preferences
    timezone = Column(String, default='UTC')
    
    # schedule preferences stored as json for flexibility
    # example: {"morning_checkin": "08:00", "evening_reflection": "22:00", "checkin_interval_minutes": 180}
    schedule_preferences = Column(JSON, default={})
    
    # personality preferences
    bot_personality = Column(String, default='friendly')  # friendly, professional, cheerful, etc

    # TODO: make preferences generic? or have schedule_preferences and preferences both json
    # preferences can hold personality but also other user customizations for bot behavior
    
    # is the user actively using the bot
    is_active = Column(Boolean, default=True)
    
    # relationships
    platform_identities = relationship("PlatformIdentity", back_populates="user")
    conversations = relationship("ConversationHistory", back_populates="user")
    memories = relationship("Memory", back_populates="user")


class PlatformIdentity(Base):
    """links platform-specific ids to our users"""
    __tablename__ = 'platform_identities'
    
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid')) # internal chordial uuid
    platform = Column(String)  # 'discord', 'telegram', 'web', etc
    platform_user_id = Column(String)  # their id on that platform
    platform_username = Column(String, nullable=True)  # their username if available
    
    created_at = Column(DateTime, default=datetime.now)
    
    # relationships
    user = relationship("User", back_populates="platform_identities")
    
    # unique constraint - one platform_user_id per platform
    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class ConversationHistory(Base):
    """stores conversation messages for persistence"""
    __tablename__ = 'conversation_history'
    
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid')) # this is the user UUID, not platform ID!
    platform = Column(String)
    
    # message data
    role = Column(String)  # 'user' or 'assistant'
    content = Column(String)
    
    message_type = Column(String)  # 'conversation' or 'scheduled'
    
    created_at = Column(DateTime, default=datetime.now)
    
    # relationships
    user = relationship("User", back_populates="conversations")
    
    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class CompressedMessage(Base):
    """stores compressed versions of messages for efficient context"""
    __tablename__ = 'compressed_messages'
    
    id = Column(Integer, primary_key=True)
    conversation_history_id = Column(Integer, ForeignKey('conversation_history.id'))
    user_uuid = Column(String, ForeignKey('users.uuid'))
    platform = Column(String)
    
    # original message info
    role = Column(String)  # 'user' or 'assistant'
    original_length = Column(Integer)  # character count of original
    
    # compressed version
    compressed_content = Column(String)
    compressed_length = Column(Integer)  # character count after compression
    compression_ratio = Column(Float)  # how much we compressed (0.3 = 70% reduction)
    
    # metadata
    created_at = Column(DateTime, default=datetime.now)
    model_used = Column(String, default='gpt-3.5-turbo')
    
    # relationships
    original_message = relationship("ConversationHistory")
    user = relationship("User")
    
    __table_args__ = (
        {'sqlite_autoincrement': True}
    )

class Memory(Base):
    """stores memories about users for persistent context"""
    __tablename__ = 'memories'
    
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    
    # core memory data
    ai_instruction = Column(String, nullable=False)  # what the ai should remember/do
    weighting = Column(Float, default=1.0)  # importance weight (higher = more important)
    keywords = Column(String)  # comma-separated keywords for searching
    
    # memory properties
    core = Column(Boolean, default=False)  # if true, always included (max weight)
    is_active = Column(Boolean, default=True)  # soft delete flag
    memory_type = Column(String(20), nullable=False)  # PREFERENCE, FACT, EPISODIC
    
    # embedding for semantic search (stored as json for sqlite compatibility)
    # for postgres, you'd use: embedding = Column(Vector(1536))
    embedding = Column(JSON)  # store as json array for now
    
    # metadata
    source = Column(String, nullable=False)  # USER_EXPLICIT, AI_INFERRED, SYSTEM_GENERATED
    access_count = Column(Integer, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    ttl = Column(Integer, nullable=True)  # time to live in seconds (null = permanent)
    memory_metadata = Column(JSON, default={})  # flexible metadata storage
    
    # relationships
    user = relationship("User", back_populates="memories")
    
    __table_args__ = (
        {'sqlite_autoincrement': True}
    )