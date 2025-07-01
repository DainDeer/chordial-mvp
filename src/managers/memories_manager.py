from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
from enum import Enum
import json
import logging

from src.database.database import get_db
from src.database.models import Memory, User

logger = logging.getLogger(__name__)


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
        memory_metadata: Optional[Dict[str, Any]] = None
    ) -> Memory:
        """create a new memory for a user"""
        
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
                "memory_metadata": memory_metadata or {}
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
    
    async def get_active_memories(
        self,
        user_uuid: str,
        memory_type: Optional[MemoryType] = None,
        include_expired: bool = False
    ) -> List[Memory]:
        """get all active memories for a user"""
        
        with get_db() as db:
            query = db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.is_active == True
            )
            
            # filter by type if specified
            if memory_type:
                query = query.filter(Memory.memory_type == memory_type.value)
            
            # filter out expired memories unless requested
            if not include_expired:
                now = datetime.now()
                # this is a bit tricky with sqlalchemy, so we'll filter in python
                memories = query.all()
                
                # filter out expired ttl memories
                active_memories = []
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
                            db.commit()
                            logger.info(f"memory {memory.id} expired, marking inactive")
                
                return active_memories
            
            return query.all()
    
    async def get_core_memories(self, user_uuid: str) -> List[Memory]:
        """get all core memories for a user (always included)"""
        
        with get_db() as db:
            return db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.core == True,
                Memory.is_active == True
            ).all()
    
    async def search_memories_by_keywords(
        self,
        user_uuid: str,
        search_terms: List[str]
    ) -> List[Memory]:
        """search memories by keywords"""
        
        with get_db() as db:
            memories = await self.get_active_memories(user_uuid)
            
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
        include_core: bool = True
    ) -> List[Dict[str, Any]]:
        """get memories formatted for inclusion in ai prompts"""
        
        formatted = []
        memory_ids_to_track = []
        
        with get_db() as db:
            # build base query
            query = db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.is_active == True
            )
            
            # get all active memories
            all_memories = query.all()
            
            # check and expire ttl memories
            now = datetime.now()
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
                    "source": memory.source
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
                memory.last_accessed_at = datetime.now()
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