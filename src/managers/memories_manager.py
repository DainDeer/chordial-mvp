from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import re
import json
import logging

from sqlalchemy import or_

from src.database.database import get_db
from src.database.models import Memory, User
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)


# v3 multi-helper memory model: one shared pool plus per-helper privates.
# 'shared' rows are visible to every helper; 'private' rows only to created_by.
_VALID_VISIBILITIES = {"shared", "private"}


# --- dedup/reinforcement tuning -------------------------------------------
# a candidate save matches an existing memory (of the same type) if EITHER the
# instruction word-sets overlap enough OR the keyword sets do. word-set Jaccard
# on the instruction cleanly separates rewordings of the same fact (~0.5+) from
# unrelated memories (~0.05), where char-level ratios were too muddy.
#
# this is deliberately a conservative FIRST pass: it catches obvious lexical
# duplicates cheaply (no api call). genuinely ambiguous cases - a fact reworded
# with little shared vocabulary, or two short facts differing by one word - are
# left for the curator agent, which has an actual model to judge them. the
# unused `embedding` column is the eventual upgrade path (cosine similarity).
_INSTRUCTION_JACCARD_THRESHOLD = 0.5
_KEYWORD_JACCARD_THRESHOLD = 0.6
_REINFORCE_WEIGHT_STEP = 1.0
_MAX_REINFORCED_WEIGHT = 10.0   # non-core rows cap here; nothing rivals core (999)

_WORD_RE = re.compile(r"[a-z0-9']+")


def _word_set(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


def _keyword_set(csv: Optional[str]) -> set:
    return {k.strip().lower() for k in (csv or "").split(",") if k.strip()}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class UpsertResult:
    """outcome of upsert_memory, so the save_memory tool can tell the model
    whether it created a new memory or reinforced an existing one."""
    memory_id: int
    action: str          # "inserted" | "reinforced"
    weighting: float
    times_seen: int      # 1 on first insert, +1 per reinforcement
    instruction: str


class MemoryType(Enum):
    """types of memories we can store"""
    PREFERENCE = "PREFERENCE"  # ai behavior preferences
    FACT = "FACT"  # facts about the user
    EPISODIC = "EPISODIC"  # events and short-term context


class MemorySource(Enum):
    """where memories come from"""
    USER_EXPLICIT = "USER_EXPLICIT"  # user told us directly
    AI_INFERRED = "AI_INFERRED"  # ai figured it out from conversation
    SYSTEM_GENERATED = "SYSTEM_GENERATED"  # system created (like onboarding)


class MemoriesManager:
    """manages user memories for persistent context"""
    
    def __init__(self):
        self.default_weighting = 1.0
        self.core_memory_weight = 999.0  # special weight for core memories
        
    async def create_memory(
        self,
        user_uuid: str,
        ai_instruction: str,
        memory_type: MemoryType,
        source: MemorySource,
        keywords: Optional[List[str]] = None,
        weighting: Optional[float] = None,
        core: bool = False,
        ttl_seconds: Optional[int] = None,
        embedding: Optional[List[float]] = None,
        memory_metadata: Optional[Dict[str, Any]] = None,
        created_by: str = 'chordial',
        visibility: str = 'shared',
    ) -> Memory:
        """create a new memory for a user"""
        if visibility not in _VALID_VISIBILITIES:
            raise ValueError(f"invalid visibility '{visibility}' - must be one of {_VALID_VISIBILITIES}")

        with get_db() as db:
            # check if user exists
            user = db.query(User).filter(User.uuid == user_uuid).first()
            if not user:
                raise ValueError(f"user {user_uuid} not found")

            # prepare memory data
            memory_data = {
                "user_uuid": user_uuid,
                "ai_instruction": ai_instruction,
                "memory_type": memory_type.value,
                "source": source.value,
                "keywords": ",".join(keywords) if keywords else "",
                "weighting": self.core_memory_weight if core else (weighting or self.default_weighting),
                "core": core,
                "ttl": ttl_seconds,
                "embedding": embedding if embedding else None,  # stored as json
                "memory_metadata": memory_metadata or {},
                "created_by": created_by,
                "visibility": visibility,
            }
            
            memory = Memory(**memory_data)
            db.add(memory)
            db.commit()
            db.refresh(memory)
            
            logger.info(
                f"created {'core' if core else 'regular'} {memory_type.value} memory "
                f"for user {user_uuid}: {ai_instruction[:50]}..."
            )

            return memory

    async def upsert_memory(
        self,
        user_uuid: str,
        ai_instruction: str,
        memory_type: MemoryType,
        source: MemorySource,
        keywords: Optional[List[str]] = None,
        core: bool = False,
        created_by: str = 'chordial',
        visibility: str = 'shared',
    ) -> UpsertResult:
        """save a memory, reinforcing an existing near-duplicate instead of
        inserting a second copy. reinforcement bumps the row's weight (so facts
        mentioned repeatedly rise in importance), unions the keywords, and marks
        it for re-curation. matching is scoped to the same memory_type AND the
        same visibility scope (a private row only reinforces another private row
        from the same helper; a shared row only reinforces another shared row) -
        the shared-pool-plus-privates model never lets a save from one helper's
        private scope quietly absorb into another's, or into the shared pool.

        core memories are always inserted verbatim - they're deliberate, and we
        never want dedup logic quietly folding one into another."""
        if visibility not in _VALID_VISIBILITIES:
            raise ValueError(f"invalid visibility '{visibility}' - must be one of {_VALID_VISIBILITIES}")

        cand_keywords = _keyword_set(",".join(keywords) if keywords else "")

        if not core:
            with get_db() as db:
                query = db.query(Memory).filter(
                    Memory.user_uuid == user_uuid,
                    Memory.is_active == True,
                    Memory.core == False,
                    Memory.memory_type == memory_type.value,
                    Memory.visibility == visibility,
                )
                if visibility == 'private':
                    query = query.filter(Memory.created_by == created_by)
                candidates = query.all()

                cand_words = _word_set(ai_instruction)
                best = None
                best_score = 0.0
                for row in candidates:
                    tsim = _jaccard(cand_words, _word_set(row.ai_instruction))
                    ksim = _jaccard(cand_keywords, _keyword_set(row.keywords))
                    if tsim >= _INSTRUCTION_JACCARD_THRESHOLD or ksim >= _KEYWORD_JACCARD_THRESHOLD:
                        score = max(tsim, ksim)
                        if score > best_score:
                            best, best_score = row, score

                if best is not None:
                    best.weighting = min(
                        _MAX_REINFORCED_WEIGHT,
                        (best.weighting or self.default_weighting) + _REINFORCE_WEIGHT_STEP,
                    )
                    best.reinforced_count = (best.reinforced_count or 0) + 1
                    best.last_reinforced_at = utc_now()
                    best.curated_at = None  # keywords/weight changed - re-review
                    merged = _keyword_set(best.keywords) | cand_keywords
                    best.keywords = ",".join(sorted(merged))
                    result = UpsertResult(
                        memory_id=best.id,
                        action="reinforced",
                        weighting=best.weighting,
                        times_seen=best.reinforced_count + 1,
                        instruction=best.ai_instruction,
                    )
                    db.commit()
                    logger.info(
                        "reinforced memory %s for user %s (weight=%.1f, seen %dx)",
                        result.memory_id, user_uuid, result.weighting, result.times_seen,
                    )
                    return result

        # no match (or a core memory) - insert fresh. new rows have curated_at
        # NULL by default, so the curator will review them. capture id/weight
        # WHILE the session is open (get_db expires orm objects on commit).
        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            if not user:
                raise ValueError(f"user {user_uuid} not found")

            memory = Memory(
                user_uuid=user_uuid,
                ai_instruction=ai_instruction,
                memory_type=memory_type.value,
                source=source.value,
                keywords=",".join(keywords) if keywords else "",
                weighting=self.core_memory_weight if core else self.default_weighting,
                core=core,
                memory_metadata={},
                created_by=created_by,
                visibility=visibility,
            )
            db.add(memory)
            db.flush()  # populate memory.id before the session closes
            result = UpsertResult(
                memory_id=memory.id,
                action="inserted",
                weighting=memory.weighting,
                times_seen=1,
                instruction=ai_instruction,
            )
            db.commit()

        logger.info(
            "created %s%s memory %s for user %s: %s",
            "core " if core else "", memory_type.value, result.memory_id,
            user_uuid, ai_instruction[:50],
        )
        return result
    
    async def get_active_memories(
        self,
        user_uuid: str,
        memory_type: Optional[MemoryType] = None,
        include_expired: bool = False,
        helper_id: Optional[str] = None,
    ) -> List[Memory]:
        """get all active memories for a user.

        helper_id, when given, applies the shared-pool-plus-privates filter:
        (visibility == 'shared') | (created_by == helper_id). None (the
        default) applies no visibility filter - backward compatible for
        callers that haven't gone multi-helper yet."""

        with get_db() as db:
            query = db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.is_active == True
            )

            # filter by type if specified
            if memory_type:
                query = query.filter(Memory.memory_type == memory_type.value)

            # shared-pool-plus-privates: every helper sees shared rows, plus
            # only their own private rows
            if helper_id is not None:
                query = query.filter(
                    or_(Memory.visibility == 'shared', Memory.created_by == helper_id)
                )

            # filter out expired memories unless requested
            if not include_expired:
                now = utc_now()
                # this is a bit tricky with sqlalchemy, so we'll filter in python
                memories = query.all()

                # filter out expired ttl memories
                active_memories = []
                newly_expired_ids = []
                for memory in memories:
                    if memory.ttl is None:  # no ttl = permanent
                        active_memories.append(memory)
                    else:
                        age_seconds = (now - memory.created_at).total_seconds()
                        if age_seconds < memory.ttl:
                            active_memories.append(memory)
                        else:
                            # mark as inactive for next time
                            memory.is_active = False
                            newly_expired_ids.append(memory.id)

                # detach the rows we're about to return BEFORE committing the
                # ttl-expiry writes: get_db() commits (and expire_on_commit
                # clears every attribute of every object still tracked by the
                # session) on exit, so returning attached instances makes them
                # raise DetachedInstanceError the moment a caller reads them
                # after this function returns. expunge first so their
                # already-loaded values survive the session closing.
                for memory in active_memories:
                    db.expunge(memory)

                if newly_expired_ids:
                    db.commit()
                    for mid in newly_expired_ids:
                        logger.info(f"memory {mid} expired, marking inactive")

                return active_memories

            rows = query.all()
            db.expunge_all()
            return rows
    
    async def get_core_memories(self, user_uuid: str) -> List[Memory]:
        """get all core memories for a user (always included).

        NOTE: these are ORM instances bound to a session that get_db() commits
        (and therefore expires) on exit. only safe to read inside a session -
        use get_core_memories_for_prompt() if you need the data afterwards.
        """
        with get_db() as db:
            return db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.core == True,
                Memory.is_active == True
            ).all()

    async def get_core_memories_for_prompt(
        self, user_uuid: str, helper_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """core memories as detached-safe dicts, sorted by id for deterministic
        (cacheable) ordering.

        extracts the fields WHILE the session is open. get_db() commits on exit,
        which expires the orm instances - returning Memory objects and reading
        their attributes afterwards raises DetachedInstanceError.

        helper_id, when given, applies the shared-pool-plus-privates filter
        (see get_active_memories); None (default) is unfiltered, for backward
        compatibility with callers that haven't gone multi-helper yet. the
        returned dicts always carry created_by so a later phase can render
        sibling attribution ("(from aria) ...") - the currently-rendered
        prompt string itself is unchanged (cache-sensitive bytes).
        """
        with get_db() as db:
            query = db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.core == True,
                Memory.is_active == True
            )
            if helper_id is not None:
                query = query.filter(
                    or_(Memory.visibility == 'shared', Memory.created_by == helper_id)
                )
            rows = query.order_by(Memory.id).all()
            return [
                {"id": m.id, "instruction": m.ai_instruction, "created_by": m.created_by}
                for m in rows
            ]
    
    async def search_memories_by_keywords(
        self,
        user_uuid: str,
        search_terms: List[str],
        helper_id: Optional[str] = None,
    ) -> List[Memory]:
        """search memories by keywords.

        helper_id, when given, applies the shared-pool-plus-privates filter
        (see get_active_memories); None (default) is unfiltered - backward
        compatible until phase-2 callers start passing helper ids."""

        with get_db() as db:
            memories = await self.get_active_memories(user_uuid, helper_id=helper_id)

            # simple keyword matching for now
            matches = []
            for memory in memories:
                if not memory.keywords:
                    continue
                
                memory_keywords = [k.strip().lower() for k in memory.keywords.split(",")]
                search_terms_lower = [term.lower() for term in search_terms]
                
                # check if any search term matches any keyword
                if any(term in memory_keywords for term in search_terms_lower):
                    matches.append(memory)
            
            return matches
    
    async def get_memories_for_prompt(
        self,
        user_uuid: str,
        max_count: int = 10,
        include_core: bool = True,
        helper_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """get memories formatted for inclusion in ai prompts.

        helper_id, when given, applies the shared-pool-plus-privates filter
        (see get_active_memories); None (default) is unfiltered - backward
        compatible until phase-2 callers start passing helper ids."""

        formatted = []
        memory_ids_to_track = []

        with get_db() as db:
            # build base query
            query = db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.is_active == True
            )
            if helper_id is not None:
                query = query.filter(
                    or_(Memory.visibility == 'shared', Memory.created_by == helper_id)
                )

            # get all active memories
            all_memories = query.all()
            
            # check and expire ttl memories
            now = utc_now()
            for memory in all_memories:
                if memory.ttl is not None:
                    age_seconds = (now - memory.created_at).total_seconds()
                    if age_seconds >= memory.ttl:
                        memory.is_active = False
                        logger.info(f"memory {memory.id} expired, marking inactive")
            
            db.commit()  # commit any expirations
            
            # separate core and regular memories
            core_memories = [m for m in all_memories if m.core and m.is_active]
            regular_memories = [m for m in all_memories if not m.core and m.is_active]
            
            # start with core memories
            memories_to_include = core_memories.copy()
            
            # sort regular memories by weight and recency
            regular_memories.sort(
                key=lambda m: (m.weighting, m.last_accessed_at or m.created_at),
                reverse=True
            )
            
            # add top regular memories up to limit
            remaining_slots = max_count - len(memories_to_include)
            memories_to_include.extend(regular_memories[:remaining_slots])
            
            # format for prompt inclusion WHILE SESSION IS OPEN
            for memory in memories_to_include:
                formatted.append({
                    "type": memory.memory_type,
                    "instruction": memory.ai_instruction,
                    "core": memory.core,
                    "source": memory.source,
                    "created_by": memory.created_by,
                })
                
                # collect ids to track access later
                memory_ids_to_track.append(memory.id)
        
        # update access tracking with separate session
        for memory_id in memory_ids_to_track:
            await self._track_memory_access(memory_id)
        
        return formatted
    
    async def _track_memory_access(self, memory_id: int):
        """update access count and timestamp for a memory"""
        
        with get_db() as db:
            memory = db.query(Memory).filter(Memory.id == memory_id).first()
            if memory:
                memory.access_count += 1
                memory.last_accessed_at = utc_now()
                db.commit()
    
    async def update_memory_weight(
        self,
        memory_id: int,
        new_weight: float
    ):
        """update the weighting of a memory"""
        
        with get_db() as db:
            memory = db.query(Memory).filter(Memory.id == memory_id).first()
            if memory and not memory.core:  # can't change core memory weights
                memory.weighting = new_weight
                db.commit()
                logger.info(f"updated memory {memory_id} weight to {new_weight}")
    
    async def deactivate_memory(self, memory_id: int):
        """soft delete a memory"""
        
        with get_db() as db:
            memory = db.query(Memory).filter(Memory.id == memory_id).first()
            if memory:
                memory.is_active = False
                db.commit()
                logger.info(f"deactivated memory {memory_id}")
    
    async def get_memory_stats(self, user_uuid: str) -> Dict[str, Any]:
        """get statistics about a user's memories"""
        
        memories = await self.get_active_memories(user_uuid, include_expired=True)
        
        stats = {
            "total_memories": len(memories),
            "active_memories": len([m for m in memories if m.is_active]),
            "core_memories": len([m for m in memories if m.core]),
            "by_type": {
                "preferences": len([m for m in memories if m.memory_type == MemoryType.PREFERENCE.value]),
                "facts": len([m for m in memories if m.memory_type == MemoryType.FACT.value]),
                "episodic": len([m for m in memories if m.memory_type == MemoryType.EPISODIC.value])
            },
            "by_source": {
                "user_explicit": len([m for m in memories if m.source == MemorySource.USER_EXPLICIT.value]),
                "ai_inferred": len([m for m in memories if m.source == MemorySource.AI_INFERRED.value]),
                "system_generated": len([m for m in memories if m.source == MemorySource.SYSTEM_GENERATED.value])
            },
            "average_access_count": sum(m.access_count for m in memories) / len(memories) if memories else 0
        }
        
        return stats