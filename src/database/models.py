from sqlalchemy import Column, String, DateTime, Date, JSON, Boolean, ForeignKey, Integer, Float, Index, UniqueConstraint, text
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

    # one platform_user_id per platform, enforced for real (the linking flow
    # relies on this - a telegram account can only be bound to one user)
    __table_args__ = (
        UniqueConstraint('platform', 'platform_user_id',
                         name='uq_platform_identity_platform_user'),
        {'sqlite_autoincrement': True},
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
    # provenance: which platform this event happened on. NOT a conversation
    # key - a user has ONE conversation spanning platforms; this just records
    # where each moment took place (delivery targeting, switch detection).
    platform = Column(String)

    author_type = Column(String, nullable=False)   # 'user' | 'agent' | 'system'
    author = Column(String, nullable=False)        # 'user' | 'chordial' | 'curator' | future personas
    kind = Column(String, nullable=False)          # 'message' | 'action' | 'note'

    # message text, or (for actions) the frozen one-line rendering that gets
    # replayed into prompts verbatim - written once, never re-serialized, so
    # history bytes stay cache-stable
    content = Column(String, nullable=False)
    message_type = Column(String, nullable=True)   # 'conversation' | 'scheduled'; only on kind='message'
    event_metadata = Column(JSON, default={})      # actions: {tool, input, result, is_error, iteration}

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_conversation_events_user_id', 'user_uuid', 'id'),
        {'sqlite_autoincrement': True},
    )


class LinkCode(Base):
    """one-time codes for linking a new platform account to an existing user.

    chordial hands the user a short code (and a telegram deep link carrying
    it); redeeming it on the new platform binds that platform identity to the
    same user - same memories, same conversation. short-lived and single-use.
    """
    __tablename__ = 'link_codes'

    id = Column(Integer, primary_key=True)
    code = Column(String, nullable=False, unique=True)   # 8 chars, unambiguous alphabet
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)        # created + ttl (15 min default)
    used_at = Column(DateTime, nullable=True)            # stamped on redemption

    __table_args__ = (
        {'sqlite_autoincrement': True}
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

    # v3 multi-helper memory model: one shared pool plus per-helper privates.
    # created_by is always set (attribution, even for shared rows - lets a
    # sibling's memory render as "(from aria) ..."). visibility='shared' means
    # every helper's search + core-memory rendering can see the row; 'private'
    # restricts it to created_by only.
    created_by = Column(String, default='chordial')
    visibility = Column(String, default='shared')  # 'shared' | 'private'

    # relationships
    user = relationship("User", back_populates="memories")

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class HelperState(Base):
    """per-(user, helper) relationship state: has this helper been met, is it
    enabled, and what identity did it take. one row per (user, helper) pair;
    the director's cast is every row with status='active'."""
    __tablename__ = 'helper_states'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    helper_id = Column(String, nullable=False)      # persona card id, e.g. 'tempo'

    # not_met | introducing | active | declined | disabled
    status = Column(String, default='not_met')

    # chosen identity (layer 2, per-user) - denormalized from the shared-visibility
    # identity core memory so the director's cast list is a plain column read.
    persona_name = Column(String, nullable=True)    # e.g. 'Ember'
    persona_form = Column(String, nullable=True)    # e.g. 'red panda' | 'no character'

    introduced_at = Column(DateTime, nullable=True)
    disabled_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('user_uuid', 'helper_id', name='uq_helper_state_user_helper'),
        {'sqlite_autoincrement': True},
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
    # which helper this call was made on behalf of (v3 per-helper cost
    # visibility); nullable/unset until later-phase writers start populating it.
    helper_id = Column(String, nullable=True)

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


# --- native workspace (docs/NATIVE_WORKSPACE_DESIGN.md) ---------------------
# the system of record for the user's workspace, replacing notion. controlled
# vocabularies live in src/services/workspace/vocab.py; every mutation goes
# through WorkspaceStore (src/services/workspace/store.py) so the invariants -
# closed_at stamping (design section 2.0), plans.last_activity_at side effects,
# goal/plan consistency, reschedule bumps - live in exactly one place.
#
# lifecycle convention (section 2.0): closable entities never hard-delete;
# status vocab splits into an open set and a closed set (closed always
# distinguishes completed from released), and closed_at is stamped by the
# store when status enters the closed set, cleared on reopen.


class Plan(Base):
    """a helper-stewarded body of work, possibly lofty/multi-month (evolves
    the dainframe's Projects). the steward (`helper`) nudges it along its
    cadence; `why` and `success_criteria` are the user's own words, raised in
    conversation rather than demanded at creation."""
    __tablename__ = 'plans'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)
    helper = Column(String, nullable=False)   # archetype id: chordial/tempo/aria/pep/mochi/poet
    status = Column(String, default='proposed')  # proposed/active/paused | complete/released

    why = Column(String, nullable=True)                # user's motivation, their words
    success_criteria = Column(String, nullable=True)   # "success looks like"
    horizon_start = Column(Date, nullable=True)        # soft range, not a deadline
    horizon_end = Column(Date, nullable=True)
    cadence = Column(String, nullable=True)            # daily/weekly/loose

    legacy_area = Column(String, nullable=True)        # preserved dainframe Area
    notion_page_id = Column(String, nullable=True)     # import provenance; never used at runtime

    # stamped by WorkspaceStore as a side effect of any related write (task
    # under the plan, win logged, note attached, check-in touching it) -
    # powers "it's been three weeks since this came up" without streaks
    last_activity_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Goal(Base):
    """a concrete milestone under a plan. `done_means` is the anti-vagueness
    field: what specifically will be true when this is done."""
    __tablename__ = 'goals'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=False)

    title = Column(String, nullable=False)
    status = Column(String, default='not_started')  # not_started/in_progress | done/renegotiated
    target = Column(Date, nullable=True)
    done_means = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Cycle(Base):
    """the bi-weekly balancing lever across plans. `focus` is pep's negotiated
    balance statement for the cycle."""
    __tablename__ = 'cycles'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)
    status = Column(String, default='upcoming')  # upcoming/active | complete
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    goal = Column(String, nullable=True)    # the cycle goal, as today
    focus = Column(String, nullable=True)   # v3: negotiated balance statement

    notion_page_id = Column(String, nullable=True)  # import provenance

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Task(Base):
    """pomodoro-sized work (evolves the dainframe Tasks db in place).
    `scheduled` is a user-local calendar date, exactly as notion stored it -
    agenda comparisons use the user's `today`. singular plan/cycle FKs replace
    notion's multi-relations (every consumer already took only the first)."""
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)

    title = Column(String, nullable=False)
    status = Column(String, default='todo')     # todo/in_progress | done/deprioritized
    priority = Column(String, nullable=True)    # high/medium/low
    scheduled = Column(Date, nullable=True)     # user-local calendar date
    window = Column(String, nullable=True)      # morning/afternoon/evening/anytime
    pom_estimate = Column(Float, nullable=True)

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    # when set, the store enforces goal.plan_id == plan_id
    goal_id = Column(Integer, ForeignKey('goals.id'), nullable=True)
    cycle_id = Column(Integer, ForeignKey('cycles.id'), nullable=True)
    helper = Column(String, nullable=True)      # who assigned/nudges

    # bumped by the store each time `scheduled` slips to a later date;
    # renegotiate (not nag) at 2-3
    reschedules = Column(Integer, default=0)
    description = Column(String, nullable=True)

    notion_page_id = Column(String, nullable=True)  # import provenance

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # stamped for both endings; wins/analytics read it where status='done'
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # the agenda's two query shapes
        Index('ix_tasks_user_status', 'user_uuid', 'status'),
        Index('ix_tasks_user_scheduled', 'user_uuid', 'scheduled'),
        {'sqlite_autoincrement': True},
    )


class Win(Base):
    """the anti-diminishment ledger: past-tense, concrete, witnessed.
    `evidence` is the user's words verbatim at the time. immutable history -
    no updated_at, no lifecycle."""
    __tablename__ = 'wins'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)      # past-tense, concrete
    date = Column(Date, nullable=False)
    helper = Column(String, nullable=False)     # who witnessed/logged it

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    # a win born from a task completion keeps the link
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)

    evidence = Column(String, nullable=True)    # the user's words, verbatim
    weight = Column(String, default='solid')    # spark/solid/milestone

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Checkin(Base):
    """the shared daily journal. morning/evening are unique per (user, date) -
    enforced by a PARTIAL unique index so adhoc check-ins stay unlimited.
    energy is asked, never demanded."""
    __tablename__ = 'checkins'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    date = Column(Date, nullable=False)
    kind = Column(String, nullable=False)       # morning/evening/adhoc
    energy = Column(String, nullable=True)      # low/ok/good/great
    notes = Column(String, nullable=True)
    # "plans touched" - JSON list of plan ids, not an association table
    # (promptable, sqlite-friendly; the Memory.embedding precedent). no FK
    # protects this, so the store resolves entries against the owner's plans.
    plan_ids = Column(JSON, default=[])
    helper = Column(String, nullable=False)     # who ran it

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('uq_checkins_user_date_kind', 'user_uuid', 'date', 'kind',
              unique=True,
              sqlite_where=text("kind IN ('morning', 'evening')"),
              postgresql_where=text("kind IN ('morning', 'evening')")),
        {'sqlite_autoincrement': True},
    )


class Note(Base):
    """the one deliberately NON-committal container (design section 2.7): a
    loose creative idea (no plan_id) or plan-attached detail. never in the
    agenda, never overdue; surfaced when work starts on its plan. no task_id
    by design - tasks are pomodoro-sized, detail belongs on the plan."""
    __tablename__ = 'notes'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    body = Column(String, nullable=False)       # the jot, user's words - only required field
    title = Column(String, nullable=True)       # auto-derived from first line when absent
    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    tags = Column(JSON, default=[])             # medium: writing/music/video/...
    helper = Column(String, nullable=True)      # domain steward who captured it

    status = Column(String, default='active')   # active | promoted/archived - no "done"
    # provenance when an idea grows up; set alongside status -> promoted
    promoted_plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    promoted_task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)

    # import provenance for --import-bodies notes (the source page whose body
    # this was) - without it, importer reruns can't tell imported from missing
    notion_page_id = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Occasion(Base):
    """a dated thing that isn't work (design section 2.8): birthdays,
    appointments, flights. informs, never nags - no status, no closed_at;
    occasions pass, they aren't done. on recurrence the store rolls `date`
    forward past occurrence, so `date` always holds the next one."""
    __tablename__ = 'occasions'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)
    date = Column(Date, nullable=False)         # user-local, same semantics as tasks.scheduled
    time = Column(String, nullable=True)        # freeform ("14:30", "afternoon") - display, not scheduling
    recurrence = Column(String, nullable=True)  # yearly/monthly/weekly; null = one-off

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    notes = Column(String, nullable=True)
    helper = Column(String, nullable=True)      # who captured it

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    # which helper this trace belongs to (v3 per-helper cost visibility);
    # nullable/unset until later-phase writers start populating it.
    helper_id = Column(String, nullable=True)

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