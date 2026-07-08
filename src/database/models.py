from sqlalchemy import Column, String, DateTime, JSON, Boolean, ForeignKey, Integer, Float, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

Base = declarative_base()

# all DateTime columns below are stored as naive UTC (datetime.utcnow), never
# server-local time - convert to a user's local timezone at the point of use
# via src.utils.timezone_utils

class User(Base):
    """main user table - our source of truth for users across platforms"""
    __tablename__ = 'users'
    
    # primary key is a uuid so we're not tied to any platform's id system
    uuid = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # what the user wants to be called
    preferred_name = Column(String, nullable=True)
    
    # when they joined
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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

    # synthetic/seed account - kept in the db for testing, but never a target
    # for proactive/outbound sends (scheduler skips it before generating)
    is_test = Column(Boolean, default=False)

    # relationships
    platform_identities = relationship("PlatformIdentity", back_populates="user")
    memories = relationship("Memory", back_populates="user")


class PlatformIdentity(Base):
    """links platform-specific ids to our users"""
    __tablename__ = 'platform_identities'
    
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid')) # internal chordial uuid
    platform = Column(String)  # 'discord', 'telegram', 'web', etc
    platform_user_id = Column(String)  # their id on that platform
    platform_username = Column(String, nullable=True)  # their username if available

    # is this specific link deliverable? flipped off when a send hard-fails
    # (discord 404/forbidden) so we stop paying to message a dead channel,
    # without deactivating the user who may still be reachable elsewhere
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    
    # relationships
    user = relationship("User", back_populates="platform_identities")
    
    # unique constraint - one platform_user_id per platform
    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class ConversationEvent(Base):
    """the conversation event log: everything that happened in a user's
    channel, in order - messages, agent tool actions, (future) system notes.

    replaces the old conversation_history table. single writer + sqlite
    autoincrement means id order IS chronological order; created_at is kept
    for humans and for the scheduler's elapsed-time math. author attribution
    (rather than a bare user/assistant role) is what lets multiple agent
    personas share one channel later without another schema change.
    """
    __tablename__ = 'conversation_events'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    platform = Column(String)

    author_type = Column(String, nullable=False)   # 'user' | 'agent' | 'system'
    author = Column(String, nullable=False)        # 'user' | 'chordial' | 'curator' | future personas
    kind = Column(String, nullable=False)          # 'message' | 'action' | 'note' (note reserved, unused)

    # message text, or (for actions) the frozen one-line rendering that gets
    # replayed into prompts verbatim - written once, never re-serialized, so
    # history bytes stay cache-stable
    content = Column(String, nullable=False)
    message_type = Column(String, nullable=True)   # 'conversation' | 'scheduled'; only on kind='message'
    event_metadata = Column(JSON, default={})      # actions: {tool, input, result, is_error, iteration}

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_conversation_events_user_platform_id', 'user_uuid', 'platform', 'id'),
        {'sqlite_autoincrement': True},
    )


class CompressedMessage(Base):
    """stores compressed versions of messages for efficient context"""
    __tablename__ = 'compressed_messages'

    id = Column(Integer, primary_key=True)
    # legacy pointer into the retired conversation_history table; new rows
    # store the conversation_events id here instead. plain integer (no FK) so
    # pre-migration ids can dangle harmlessly.
    conversation_history_id = Column(Integer)
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
    created_at = Column(DateTime, default=datetime.utcnow)
    model_used = Column(String, default='gpt-3.5-turbo')

    # relationships
    user = relationship("User")

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )

class ConversationSummary(Base):
    """stores summaries of conversation chunks for compressed long-term context"""
    __tablename__ = 'conversation_summaries'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'))
    platform = Column(String)

    # range of conversation_history ids this summary covers
    first_message_id = Column(Integer)
    last_message_id = Column(Integer)
    message_count = Column(Integer)

    summary = Column(String)
    key_topics = Column(JSON, default=[])
    model_used = Column(String)

    created_at = Column(DateTime, default=datetime.utcnow)

    # relationships
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
    created_at = Column(DateTime, default=datetime.utcnow)
    ttl = Column(Integer, nullable=True)  # time to live in seconds (null = permanent)
    memory_metadata = Column(JSON, default={})  # flexible metadata storage

    # reinforcement: each time a duplicate memory is saved we bump the existing
    # row instead of inserting a new one, so repeated facts grow in importance.
    reinforced_count = Column(Integer, default=0)   # times this memory was re-saved
    last_reinforced_at = Column(DateTime, nullable=True)

    # curation: the curator agent reviews rows where curated_at IS NULL (merge /
    # update / expire / promote), then stamps them. merged_into points at the
    # canonical row when this one was absorbed by a merge (soft-deleted too).
    curated_at = Column(DateTime, nullable=True)
    merged_into = Column(Integer, nullable=True)

    # relationships
    user = relationship("User", back_populates="memories")

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class AgendaSnapshot(Base):
    """rolling cache of a user's notion 'today picture' (see
    services/notion/snapshot_service.py).

    one row per user, overwritten in place: the scheduler keeps it fresh in the
    background and the chat path reads the pre-rendered `digest` as ambient
    context (a db read, never a synchronous notion call). history worth diffing
    is frozen in daily_notes, not kept here.
    """
    __tablename__ = 'agenda_snapshots'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, unique=True)

    # structured agenda rows (cycle / projects / tasks_today / tasks_overdue /
    # tasks_in_progress / done_today) - what the daily passes diff against.
    payload = Column(JSON, default={})
    # pre-rendered ~150-400 token text - what conversation turns inject verbatim.
    digest = Column(String, nullable=True)

    refreshed_at = Column(DateTime, nullable=True)
    # flipped True by notion write tools so the next background pass re-fetches.
    is_stale = Column(Boolean, default=True)
    # last refresh failure (kept for debugging; the digest survives the error).
    last_error = Column(String, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class UsageLog(Base):
    """per-call token accounting - one row per ai api call.

    the foundation for per-user cost visibility and (later) daily budgets.
    cheap to write now, impossible to backfill later.
    """
    __tablename__ = 'usage_log'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=True)
    platform = Column(String, nullable=True)

    provider = Column(String)   # 'anthropic' | 'openai'
    model = Column(String)
    role = Column(String)       # 'conversation' | 'scheduled' | 'utility'

    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class AgentTrace(Base):
    """one row per agent turn - records the tool loop for debugging/tuning.

    kept separate from the conversation event log so raw intra-turn tool
    exchanges stay out of the replayed (and cached) conversation prefix.
    """
    __tablename__ = 'agent_traces'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=True)
    platform = Column(String, nullable=True)

    turn_kind = Column(String)          # 'conversation' | 'scheduled'
    iterations = Column(Integer, default=0)
    hit_iteration_cap = Column(Boolean, default=False)
    # list of {iteration, calls: [{name, input, is_error}]}
    tool_trace = Column(JSON, default=[])
    final_text_length = Column(Integer, default=0)
    stop_reason = Column(String, nullable=True)

    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    total_cache_read_tokens = Column(Integer, default=0)
    total_cache_write_tokens = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )