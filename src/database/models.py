from sqlalchemy import Column, String, DateTime, JSON, Boolean, ForeignKey, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

Base = declarative_base()

class User(Base):
    """main user table - our source of truth for users across platforms"""
    __tablename__ = 'users'
    
    # primary key is a uuid so we're not tied to any platform's id system
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
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
    
    # is the user actively using the bot
    is_active = Column(Boolean, default=True)
    
    # relationships
    platform_identities = relationship("PlatformIdentity", back_populates="user")
    conversations = relationship("ConversationHistory", back_populates="user")


class PlatformIdentity(Base):
    """links platform-specific ids to our users"""
    __tablename__ = 'platform_identities'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(String, ForeignKey('users.id'))
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
    user_id = Column(String, ForeignKey('users.id'))
    platform = Column(String)
    
    # message data
    role = Column(String)  # 'user' or 'assistant'
    content = Column(String)
    
    # context stored as json - includes temporal context and message metadata
    # example: {"time_of_day": "morning", "message_type": "scheduled", ...}
    context = Column(JSON)
    
    created_at = Column(DateTime, default=datetime.now)
    
    # relationships
    user = relationship("User", back_populates="conversations")
    
    __table_args__ = (
        {'sqlite_autoincrement': True}
    )