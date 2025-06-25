import logging
from typing import Optional, List, Dict
from datetime import datetime

from src.database.database import get_db
from src.database.models import ConversationHistory, CompressedMessage
from src.ai.openai_provider import OpenAIProvider
from src.services.prompt_service import PromptService

logger = logging.getLogger(__name__)

class CompressorService:
    """compresses messages in real-time for efficient context management"""
    
    def __init__(self, compression_model: str = "gpt-3.5-turbo"):
        self.compressor = OpenAIProvider(model=compression_model)
        self.target_compression_ratio = 0.3  # aim for 70% reduction
        self.min_length_to_compress = 100  # don't compress tiny messages
    
    async def compress_message(
        self, 
        content: str, 
        role: str,
        preserve_key_info: bool = True # TODO: unused arg
    ) -> str:
        """compress a single message while preserving key information"""
        
        # don't compress short messages
        if len(content) < self.min_length_to_compress:
            return content
        
        try:
            # use prompt service for consistent prompt construction
            prompt_service = PromptService()
            
            # different compression instructions for user vs assistant
            if role == "user":
                compression_instructions = """You are a message compressor. Compress the user's message to its essential meaning.
Keep: intentions, questions, emotional tone, specific requests
Remove: filler words, repetition, unnecessary details
Output only the compressed message, no explanation.

Try to compress this to 30-50 words maximum while keeping the core meaning."""
                
            else:  # assistant message
                compression_instructions = """You are a message compressor. Compress this AI assistant response to its key points.
Keep: main advice/information, commitments, important context
Remove: pleasantries, repetition, examples (unless critical)
Maintain the assistant's helpful tone but be very concise.
Output only the compressed message, no explanation.

Try to compress this to 50-75 words maximum while keeping essential information."""
            
            # build prompt using our service
            messages = prompt_service.build_custom_prompt(
                system_instructions=compression_instructions,
                user_message=content
            )
            
            # use the compressor model
            compressed = await self.compressor.generate_response(messages)
            
            # make sure we actually compressed it
            if len(compressed) >= len(content) * 0.8:
                logger.warning(f"compression failed to reduce size significantly")
                # could try more aggressive compression here if needed
            
            return compressed.strip()
            
        except Exception as e:
            logger.error(f"error compressing message: {e}")
            # fallback: just truncate
            return content[:200] + "..." if len(content) > 200 else content
        
    async def store_compressed_message(
        self,
        conversation_history_id: int,
        user_uuid: str,
        platform: str,
        role: str,
        original_content: str,
        compressed_content: str
    ) -> CompressedMessage:
        """store a compressed message in the database"""
        with get_db() as db:
            compressed_msg = CompressedMessage(
                conversation_history_id=conversation_history_id,
                user_uuid=user_uuid,
                platform=platform,
                role=role,
                original_length=len(original_content),
                compressed_content=compressed_content,
                compressed_length=len(compressed_content),
                compression_ratio=len(compressed_content) / len(original_content) if len(original_content) > 0 else 1.0,
                model_used=self.compressor.model
            )
            db.add(compressed_msg)
            db.commit()
            
            logger.info(
                f"compressed {role} message: {len(original_content)} -> {len(compressed_content)} chars "
                f"({compressed_msg.compression_ratio:.1%} of original)"
            )
            
            return compressed_msg
    
    async def get_compressed_history(
        self,
        user_uuid: str,
        platform: str,
        limit: int = 10
    ) -> List[Dict[str, str]]:
        """get compressed conversation history for feeding to AI"""
        with get_db() as db:
            # get most recent compressed messages
            compressed_messages = db.query(CompressedMessage).filter(
                CompressedMessage.user_uuid == user_uuid,
                CompressedMessage.platform == platform
            ).order_by(CompressedMessage.created_at.desc()).limit(limit).all()
            
            # reverse to get chronological order
            compressed_messages.reverse()
            
            # convert to conversation format
            history = []
            for msg in compressed_messages:
                history.append({
                    "role": msg.role,
                    "content": msg.compressed_content,
                })
            # TODO: add time logic call here?
            return history
    
    async def get_compression_stats(self, user_uuid: str, platform: str) -> Dict:
        """get compression statistics for a user"""
        with get_db() as db:
            messages = db.query(CompressedMessage).filter(
                CompressedMessage.user_uuid == user_uuid,
                CompressedMessage.platform == platform
            ).all()
            
            if not messages:
                return {
                    "total_messages": 0,
                    "total_original_chars": 0,
                    "total_compressed_chars": 0,
                    "average_compression_ratio": 0
                }
            
            total_original = sum(m.original_length for m in messages)
            total_compressed = sum(m.compressed_length for m in messages)
            
            return {
                "total_messages": len(messages),
                "total_original_chars": total_original,
                "total_compressed_chars": total_compressed,
                "average_compression_ratio": total_compressed / total_original if total_original > 0 else 0,
                "space_saved": total_original - total_compressed
            }